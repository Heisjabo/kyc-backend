"""Deciding verifications the checks could not decide on their own.

A verification lands in needs_review when a step comes back for review (an AML
match, a document or address the checks were unsure of). Staff can:

- **approve** it: it passes, as if every step had;
- **decline** it, with a reason: it fails;
- **ask for a redo** of chosen steps: the same link opens again for those steps
  only, for a set number of hours, and the verification goes back to in
  progress. Redoing the document also redoes the AML screening, since that is
  run on the name the document gives.

Each decision is kept in audit_events (review.approved, review.declined,
review.redo_requested) against the verification, and the client's callback
is sent the new status, as it is when the checks decide.
"""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import console_role
from app.db.models import Account, AuditEvent, Step, StepStatus, Verification, VerificationStatus, VerificationStep
from app.db.session import get_session
from app.schemas.console import (
    ConsoleRedoRequest,
    ConsoleReviewDecisionRequest,
    ConsoleReviewEvent,
    ConsoleReviewResult,
)
from app.services import verifications as service

router = APIRouter(prefix="/console/verifications", tags=["admin console"])

# The console roles that may decide a verification.
REVIEWERS = ("owner", "admin", "reviewer")

# Steps a person can be asked to do again. AML is not one: it is not something
# the person does, and it is rerun with the document.
REDOABLE = (
    VerificationStep.document,
    VerificationStep.face,
    VerificationStep.address,
    VerificationStep.due_diligence,
)

REVIEW_EVENT = "review."


def _verification(db: Session, verification_id: int) -> Verification:
    verification = db.get(Verification, verification_id)
    if verification is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No verification with that id")
    return verification


def _staff_name(account: Account) -> str:
    return f"{account.first_name} {account.last_name}".strip() or account.email


def _record(db: Session, verification: Verification, staff: Account, action: str, note: str | None,
            steps: list[str] | None = None, **extra) -> AuditEvent:
    event = AuditEvent(
        event_type=REVIEW_EVENT + action,
        actor=str(staff.id),
        verification_id=verification.id,
        applicant_id=verification.applicant_id,
        payload={
            "reference": verification.reference,
            "by": staff.email,
            "note": note,
            "steps": steps or [],
            "from_status": verification.status.value,
            **extra,
        },
    )
    db.add(event)
    return event


def _review(event: AuditEvent, staff: Account) -> ConsoleReviewEvent:
    return ConsoleReviewEvent(
        action=event.event_type.removeprefix(REVIEW_EVENT),
        by=_staff_name(staff),
        note=event.payload.get("note"),
        steps=event.payload.get("steps") or [],
        at=event.created_at,
    )


def reviews_of(db: Session, verification_id: int) -> list[ConsoleReviewEvent]:
    """Every decision staff made on this verification, oldest first."""
    events = db.scalars(
        select(AuditEvent)
        .where(AuditEvent.verification_id == verification_id, AuditEvent.event_type.startswith(REVIEW_EVENT))
        .order_by(AuditEvent.created_at, AuditEvent.id)
    ).all()
    ids = {int(e.actor) for e in events if e.actor.isdigit()}
    staff = {a.id: a for a in db.scalars(select(Account).where(Account.id.in_(ids)))} if ids else {}
    return [
        ConsoleReviewEvent(
            action=e.event_type.removeprefix(REVIEW_EVENT),
            by=_staff_name(staff[int(e.actor)]) if e.actor.isdigit() and int(e.actor) in staff else e.payload.get("by") or e.actor,
            note=e.payload.get("note"),
            steps=e.payload.get("steps") or [],
            at=e.created_at,
        )
        for e in events
    ]


def _note(body: ConsoleReviewDecisionRequest | ConsoleRedoRequest | None) -> str | None:
    return ((body.note or "").strip() or None) if body else None


def _require_review(verification: Verification) -> None:
    if verification.status != VerificationStatus.needs_review:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Only a verification in review can be decided; this one is {verification.status.value}")


def _decide(db: Session, verification: Verification, outcome: VerificationStatus, event: str) -> None:
    verification.status = outcome
    verification.event = event
    verification.completed_at = datetime.now(UTC)


@router.post("/{verification_id}/approve", response_model=ConsoleReviewResult)
def approve(
    verification_id: int,
    body: ConsoleReviewDecisionRequest | None = None,
    staff: Account = Depends(console_role("kyc", REVIEWERS)),
    db: Session = Depends(get_session),
) -> ConsoleReviewResult:
    """Pass a verification that was waiting for review."""
    verification = _verification(db, verification_id)
    _require_review(verification)
    event = _record(db, verification, staff, "approved", _note(body))
    _decide(db, verification, VerificationStatus.passed, service.EVENT_ACCEPTED)
    db.commit()
    db.refresh(verification)
    db.refresh(event)
    service.notify_client(db, verification)
    return ConsoleReviewResult(status=verification.status.value, event=verification.event, review=_review(event, staff))


@router.post("/{verification_id}/decline", response_model=ConsoleReviewResult)
def decline(
    verification_id: int,
    body: ConsoleReviewDecisionRequest,
    staff: Account = Depends(console_role("kyc", REVIEWERS)),
    db: Session = Depends(get_session),
) -> ConsoleReviewResult:
    """Fail a verification that was waiting for review. A reason is required."""
    verification = _verification(db, verification_id)
    _require_review(verification)
    note = _note(body)
    if not note:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Say why it is declined")
    event = _record(db, verification, staff, "declined", note)
    _decide(db, verification, VerificationStatus.failed, service.EVENT_DECLINED)
    verification.reasons = list(dict.fromkeys([*(verification.reasons or []), "DECLINED_BY_REVIEWER"]))
    db.commit()
    db.refresh(verification)
    db.refresh(event)
    service.notify_client(db, verification)
    return ConsoleReviewResult(status=verification.status.value, event=verification.event, review=_review(event, staff))


@router.post("/{verification_id}/redo", response_model=ConsoleReviewResult)
def request_redo(
    verification_id: int,
    body: ConsoleRedoRequest,
    staff: Account = Depends(console_role("kyc", REVIEWERS)),
    db: Session = Depends(get_session),
) -> ConsoleReviewResult:
    """Ask the person to do chosen steps again on the same link.

    Each chosen step gets a new, pending attempt; the earlier attempts are kept.
    The link works again for `hours`, and the verification is in progress until
    the person finishes, when it is decided as usual.
    """
    verification = _verification(db, verification_id)
    if verification.status not in (VerificationStatus.needs_review, VerificationStatus.failed):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"A redo can be asked for on a verification in review or failed; this one is {verification.status.value}",
        )

    chosen: list[VerificationStep] = []
    for name in dict.fromkeys(body.steps):
        try:
            step = VerificationStep(name)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"There is no {name} step") from exc
        if step not in REDOABLE:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"The {name} step cannot be redone by the person")
        if name not in verification.services:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"This verification does not include the {name} step")
        chosen.append(step)
    # The AML screening is run on the name the document gives.
    if VerificationStep.document in chosen and VerificationStep.aml.value in verification.services:
        chosen.append(VerificationStep.aml)

    latest: dict[VerificationStep, Step] = {}
    for row in db.scalars(select(Step).where(Step.verification_id == verification.id)):
        if row.step not in latest or row.attempt > latest[row.step].attempt:
            latest[row.step] = row
    for step in chosen:
        current = latest.get(step)
        if current is not None and current.status == StepStatus.pending:
            continue
        db.add(Step(verification_id=verification.id, step=step, attempt=(current.attempt + 1) if current else 1, status=StepStatus.pending))

    names = [s.value for s in chosen]
    expires_at = datetime.now(UTC) + timedelta(hours=body.hours)
    event = _record(db, verification, staff, "redo_requested", _note(body), names, hours=body.hours)
    verification.status = VerificationStatus.in_progress
    verification.event = service.EVENT_PENDING
    verification.completed_at = None
    verification.expires_at = expires_at
    db.commit()
    db.refresh(verification)
    db.refresh(event)
    service.notify_client(db, verification)
    return ConsoleReviewResult(
        status=verification.status.value,
        event=verification.event,
        session_url=verification.session_url,
        expires_at=verification.expires_at,
        review=_review(event, staff),
    )
