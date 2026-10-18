"""Creating and moving a verification along.

A verification is created the moment a client initiates one, before the person
has done anything, so there is a row to attach everything else to: the steps they
walk through, the images they submit, the device they used, and the callbacks we
sent. That is also what makes the session survive being opened on a phone and
finished on a laptop, since both are looking at the same row.

The vocabulary follows Shufti's, both for status and for the callback events, so
a client already integrated with them can point at us with almost no change.
"""

import secrets
import string
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.services import platform_settings
from app.engines.fields import to_alpha3
from app.pipeline.ai_image import AI_GENERATED
from app.db.models import (
    Applicant,
    Step,
    StepStatus,
    Verification,
    VerificationStatus,
    VerificationStep,
)

# Shufti's event vocabulary. Kept as plain strings rather than an enum because
# they travel to clients, and a client should be able to match on the string
# they already match on today.
EVENT_RECEIVED = "request.received"
EVENT_PENDING = "request.pending"
EVENT_ACCEPTED = "verification.accepted"
EVENT_DECLINED = "verification.declined"
EVENT_CANCELLED = "verification.cancelled"
EVENT_TIMEOUT = "request.timeout"
EVENT_STATUS_CHANGED = "verification.status.changed"

_ALPHABET = string.ascii_uppercase + string.digits


def new_customer_id() -> str:
    """The id a person is known by, in the shape Shufti shows on its report.

    Upper case and digits, no lower case and no punctuation, so it survives being
    read down a phone line or typed from a printout.
    """
    return "".join(secrets.choice(_ALPHABET) for _ in range(22))


def new_reference() -> str:
    """A reference for a client who did not send one of their own."""
    return f"KYC_{datetime.now(UTC):%Y%m%d}_{secrets.token_hex(8).upper()}"


def find_or_create_applicant(
    db: Session,
    account_id,
    client_customer_id: str | None,
    claimed: dict,
) -> Applicant:
    """The person this verification is about.

    When the client sends their own id for the person, a returning applicant is
    found rather than duplicated, which is what keeps the duplicate face warning
    meaningful: it then only fires when a face belongs to somebody else.

    Without that id, every attempt is a new applicant. That is allowed, and the
    duplicate warning on a retry is the price of not being told who this is.
    """
    # No client id is taken at the moment: one reference per verification is what
    # a client sends, so every verification opens a new applicant, and a face
    # seen twice is what links them. See the duplicate face warning.
    if client_customer_id:
        existing = db.scalar(
            select(Applicant).where(
                Applicant.account_id == account_id,
                Applicant.client_customer_id == client_customer_id,
            )
        )
        if existing is not None:
            return existing

    applicant = Applicant(
        account_id=account_id,
        customer_id=new_customer_id(),
        client_customer_id=client_customer_id,
        # Claimed values seed the record so a person has a name before any
        # document is read. A passing document overwrites them with what it
        # proved; until then they are only what somebody typed.
        first_name=claimed.get("first_name"),
        last_name=claimed.get("last_name"),
        date_of_birth=claimed.get("date_of_birth"),
        # "RW", "RWA" and "Rwanda" all mean the same country, and the applicant
        # record keeps the code. What the client actually typed stays on the
        # verification, in `claimed`.
        country=to_alpha3(claimed.get("country")),
        email=claimed.get("email"),
        phone=claimed.get("phone"),
    )
    db.add(applicant)
    db.flush()
    return applicant


def create_verification(
    db: Session,
    account_id,
    *,
    base_url: str,
    services: list[str],
    claimed: dict,
    reference: str | None = None,
    client_customer_id: str | None = None,
    callback_url: str | None = None,
    redirect_url: str | None = None,
    country: str | None = None,
    language: str | None = None,
    verification_mode: str | None = None,
    form_id: str | None = None,
    ttl_minutes: int | None = None,
    settings_echo: dict | None = None,
) -> Verification:
    """Open a session. Everything else in the flow hangs off what this returns."""
    applicant = find_or_create_applicant(db, account_id, client_customer_id, claimed)

    token = secrets.token_urlsafe(32)
    ttl = ttl_minutes or platform_settings.value("session_ttl_minutes")
    domain = settings.VERIFY_BASE_URL.strip() if settings.VERIFY_BASE_URL else base_url

    verification = Verification(
        account_id=account_id,
        applicant_id=applicant.id,
        reference=reference or new_reference(),
        session_token=token,
        session_url=f"{domain.rstrip('/')}/verify/{token}",
        status=VerificationStatus.pending,
        event=EVENT_RECEIVED,
        services=services,
        form_id=form_id,
        callback_url=callback_url,
        redirect_url=redirect_url,
        country=country,
        language=language,
        verification_mode=verification_mode,
        claimed=claimed,
        settings=settings_echo or {},
        expires_at=datetime.now(UTC) + timedelta(minutes=ttl),
    )
    db.add(verification)
    db.flush()

    # One pending step per service, in the order asked for, so the session page
    # knows what to ask next and the client can see what is outstanding.
    for name in services:
        db.add(
            Step(
                verification_id=verification.id,
                step=VerificationStep(name),
                attempt=1,
                status=StepStatus.pending,
            )
        )
    db.commit()
    db.refresh(verification)
    return verification


def by_token(db: Session, token: str) -> Verification | None:
    return db.scalar(select(Verification).where(Verification.session_token == token))


def by_reference(db: Session, account_id, reference: str) -> Verification | None:
    """Look a verification up by the client's own reference.

    Scoped to the account: two clients may use the same string for different
    people, and neither may read the other's.
    """
    return db.scalar(
        select(Verification).where(
            Verification.reference == reference, Verification.account_id == account_id
        )
    )


def is_expired(verification: Verification, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    if verification.expires_at is None:
        return False
    expires = verification.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return expires < now


def expire_if_due(db: Session, verification: Verification) -> Verification:
    """Move a verification to expired once its link has run out.

    Checked whenever anybody looks at or acts on it, so a client polling for
    status sees the change without waiting for a sweeper, and somebody opening
    a stale link is told plainly rather than being walked into a dead session.
    A finished verification is never reopened by this.
    """
    open_states = (VerificationStatus.pending, VerificationStatus.in_progress)
    if verification.status in open_states and is_expired(verification):
        verification.status = VerificationStatus.expired
        verification.event = EVENT_TIMEOUT
        verification.completed_at = datetime.now(UTC)
        db.commit()
        db.refresh(verification)
    return verification


def expire_overdue(db: Session, limit: int = 500) -> int:
    """Sweep sessions nobody came back to. Returns how many were closed.

    Lazy expiry covers anyone still looking; this covers the ones abandoned in
    silence, so they do not sit as in_progress for ever.
    """
    overdue = db.scalars(
        select(Verification)
        .where(
            Verification.status.in_([VerificationStatus.pending, VerificationStatus.in_progress]),
            Verification.expires_at < datetime.now(UTC),
        )
        .limit(limit)
    ).all()
    for verification in overdue:
        verification.status = VerificationStatus.expired
        verification.event = EVENT_TIMEOUT
        verification.completed_at = datetime.now(UTC)
    db.commit()
    return len(overdue)


ATTEMPTS_EXHAUSTED = "ATTEMPTS_EXHAUSTED"


def attempts_exhausted(db: Session, verification: Verification, step: VerificationStep) -> bool:
    """True when this step has been failed MAX_STEP_ATTEMPTS times.

    The first time that is noticed the verification is sent to a person (needs_review, with
    ATTEMPTS_EXHAUSTED among its reasons) and the client is told, so a person cannot keep
    submitting documents until one passes.
    """
    failed = db.scalar(
        select(func.count())
        .select_from(Step)
        .where(
            Step.verification_id == verification.id,
            Step.step == step,
            Step.status == StepStatus.failed,
        )
    ) or 0
    if failed < settings.MAX_STEP_ATTEMPTS:
        return False
    if verification.status != VerificationStatus.needs_review:
        verification.status = VerificationStatus.needs_review
        verification.event = EVENT_STATUS_CHANGED
        verification.completed_at = datetime.now(UTC)
        verification.reasons = list(dict.fromkeys([*(verification.reasons or []), ATTEMPTS_EXHAUSTED]))
        db.commit()
        notify_client(db, verification)
    return True


def open_step(db: Session, verification: Verification, step: VerificationStep) -> Step:
    """The row this attempt will be recorded on.

    A pending row is reused; a finished one means this is a retry, so a new row
    is opened with the next attempt number. That is what lets a report say
    somebody failed twice on glare and passed on the third try.
    """
    rows = db.scalars(
        select(Step)
        .where(Step.verification_id == verification.id, Step.step == step)
        .order_by(Step.attempt)
    ).all()

    if rows and rows[-1].status == StepStatus.pending:
        current = rows[-1]
    else:
        current = Step(
            verification_id=verification.id,
            step=step,
            attempt=(rows[-1].attempt + 1) if rows else 1,
            status=StepStatus.pending,
        )
        db.add(current)

    current.started_at = current.started_at or datetime.now(UTC)
    if verification.started_at is None:
        verification.started_at = datetime.now(UTC)
    if verification.status == VerificationStatus.pending:
        verification.status = VerificationStatus.in_progress
        verification.event = EVENT_PENDING
    db.flush()
    return current


def ai_generated_flag(result: dict | None) -> bool | None:
    """Did this step see a generated image? Read back out of its own checks.

    True when one scored at or above the detect threshold, false when they were
    scored and none did, null when the step could not look: no image large enough,
    or the detector not installed.
    """
    for check in (result or {}).get("checks", []):
        if check.get("name") != "ai_generated":
            continue
        if check.get("status") == "skipped":
            return None
        return AI_GENERATED in (check.get("warnings") or [])
    return None


def finish_step(
    db: Session,
    step: Step,
    *,
    status: StepStatus,
    reasons: list[str] | None = None,
    warnings: list[str] | None = None,
    result: dict | None = None,
    extracted: dict | None = None,
) -> Step:
    step.status = status
    step.reasons = reasons or []
    step.warnings = warnings or []
    step.result = result or {}
    step.extracted = extracted or {}
    # Taken from the step's own checks rather than passed in by each route, so a
    # step added later cannot forget to record it.
    step.ai_generated = ai_generated_flag(step.result)
    step.completed_at = datetime.now(UTC)
    db.flush()
    return step


def settle(db: Session, verification: Verification) -> Verification:
    """Decide where the verification as a whole stands, from its steps.

    One failed step fails the verification, mirroring Shufti's
    decline_on_single_step. Anything still pending leaves it in progress.
    """
    steps = db.scalars(select(Step).where(Step.verification_id == verification.id)).all()
    latest = {}
    for step in steps:
        current = latest.get(step.step)
        if current is None or step.attempt > current.attempt:
            latest[step.step] = step
    from app.screening.yente import run_pending
    run_pending(db, verification, latest)
    outcomes = [step.status for step in latest.values()]

    reasons = [reason for step in latest.values() for reason in step.reasons]
    warnings = [warning for step in latest.values() for warning in step.warnings]

    if StepStatus.failed in outcomes:
        verification.status = VerificationStatus.failed
        verification.event = EVENT_DECLINED
        verification.completed_at = datetime.now(UTC)
    elif StepStatus.pending in outcomes:
        verification.status = VerificationStatus.in_progress
        verification.event = EVENT_PENDING
    elif StepStatus.review in outcomes:
        verification.status = VerificationStatus.needs_review
        verification.event = EVENT_STATUS_CHANGED
        verification.completed_at = datetime.now(UTC)
    else:
        verification.status = VerificationStatus.passed
        verification.event = EVENT_ACCEPTED
        verification.completed_at = datetime.now(UTC)

    verification.reasons = list(dict.fromkeys(reasons))
    verification.warnings = list(dict.fromkeys(warnings))
    db.commit()
    db.refresh(verification)

    _fire_callback(db, verification)

    return verification


def document_group(verification: Verification, step: Step | None = None) -> dict | None:
    """The document section the person ticked, as {key, label}, or None.

    Read off the latest document attempt (or the step given), with its label
    from the sections the client set, so a reader sees "I am a foreigner living
    or working in my registered country" and not only foreign_resident.
    """
    if step is None:
        attempts = [s for s in verification.steps if s.step == VerificationStep.document]
        step = max(attempts, key=lambda s: s.attempt) if attempts else None
    key = (step.extracted or {}).get("document_group") if step else None
    if not key:
        return None
    groups = (verification.settings or {}).get("document_groups") or []
    label = next((group.get("label") for group in groups if group.get("key") == key), None)
    return {"key": key, "label": label}


def notify_client(db: Session, verification: Verification) -> None:
    """Tell the client's callback where the verification stands now."""
    _fire_callback(db, verification)


def _fire_callback(db: Session, verification: Verification) -> None:
    if not verification.callback_url:
        return

    steps_data = []
    for step in verification.steps:
        steps_data.append({
            "step": step.step.value if hasattr(step.step, "value") else str(step.step),
            "status": step.status.value if hasattr(step.status, "value") else str(step.status),
            "reasons": step.reasons or [],
        })

    aml_steps = [step for step in verification.steps if step.step == VerificationStep.aml]
    aml = max(aml_steps, key=lambda step: step.attempt).result if aml_steps else None
    payload = {
        "aml": aml or None,
        "reference": verification.reference,
        "status": verification.status.value if hasattr(verification.status, "value") else str(verification.status),
        "event": verification.event,
        "reasons": verification.reasons or [],
        "steps": steps_data,
        "document_group": document_group(verification),
    }

    url = verification.callback_url
    account_id = verification.account_id

    def _send():
        import json
        import logging
        import time

        import httpx

        from app.core import security, urls

        log = logging.getLogger(__name__)
        try:
            # Checked again now: the address can have been changed to point inside
            # since the verification was made.
            urls.check_callback(url)
        except urls.UnsafeUrl as problem:
            log.warning("Callback not sent for %s: %s", verification.reference, problem)
            return
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str).encode()
        timestamp = int(time.time())
        headers = {
            "Content-Type": "application/json",
            "X-KYC-Timestamp": str(timestamp),
            "X-KYC-Signature": security.sign_callback(account_id, body, timestamp),
        }
        try:
            # No redirects: a 302 to an internal address would undo the check above.
            with httpx.Client(timeout=5.0, follow_redirects=False, trust_env=False) as client:
                client.post(url, content=body, headers=headers)
        except Exception as e:
            log.warning("Callback POST for %s failed: %s", verification.reference, e)

    import threading
    threading.Thread(target=_send, daemon=True).start()

