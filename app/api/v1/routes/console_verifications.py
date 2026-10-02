"""Every verification, for Pangolin staff in the admin console.

The client API shows a client its own verifications by reference. Staff need all
of them, across every client, so these endpoints are keyed by id (a reference is
only unique within one client) and open to console accounts with access to
Pangolin KYC.

A session whose link has run out is reported as expired even before anyone has
looked at it, by working the status out in the query rather than waiting for the
row to be updated. Listing writes nothing.
"""

from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import String, and_, case, cast, func, literal, or_, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import console_product
from app.db.models import (
    Account,
    Applicant,
    DocumentScan,
    Verification,
    VerificationFile,
    VerificationStatus,
)
from app.db.session import get_session
from app.schemas.console import (
    ConsoleApplicant,
    ConsoleCheck,
    ConsoleClient,
    ConsoleDocument,
    ConsoleStep,
    ConsoleVerificationDetail,
    ConsoleVerificationList,
    ConsoleVerificationSummary,
)
from app.services import verifications as service

router = APIRouter(prefix="/console/verifications", tags=["admin console"])

_OPEN = (VerificationStatus.pending, VerificationStatus.in_progress)
_STATUSES = [s.value for s in VerificationStatus]

# The status as a client would see it now: an open session past its expiry is expired.
_EFFECTIVE_STATUS = case(
    (
        and_(
            Verification.status.in_(_OPEN),
            Verification.expires_at.is_not(None),
            Verification.expires_at < func.now(),
        ),
        literal(VerificationStatus.expired.value),
    ),
    else_=cast(Verification.status, String),
)


def _effective(verification: Verification) -> str:
    if verification.status in _OPEN and service.is_expired(verification):
        return VerificationStatus.expired.value
    return verification.status.value


def _client(account: Account) -> ConsoleClient:
    return ConsoleClient(
        id=str(account.id),
        name=f"{account.first_name} {account.last_name}".strip(),
        email=account.email,
    )


def _applicant(verification: Verification, scan: DocumentScan | None) -> ConsoleApplicant:
    applicant = verification.applicant
    claimed = verification.claimed or {}
    first = (applicant.first_name if applicant else None) or claimed.get("first_name")
    last = (applicant.last_name if applicant else None) or claimed.get("last_name")
    name = " ".join(part for part in (first, last) if part) or (scan.full_name if scan else None)
    return ConsoleApplicant(
        customer_id=applicant.customer_id if applicant else None,
        name=name,
        email=(applicant.email if applicant else None) or claimed.get("email"),
        country=(applicant.country if applicant else None) or verification.country,
    )


def _summary(verification: Verification, scan: DocumentScan | None) -> ConsoleVerificationSummary:
    return ConsoleVerificationSummary(
        id=str(verification.id),
        reference=verification.reference,
        status=_effective(verification),
        event=verification.event,
        services=verification.services,
        document_type=scan.document_type.value if scan else None,
        reasons=verification.reasons,
        client=_client(verification.account),
        applicant=_applicant(verification, scan),
        created_at=verification.created_at,
        completed_at=verification.completed_at,
        expires_at=verification.expires_at,
    )


def _latest_scans(db: Session, verification_ids: list[int]) -> dict[int, DocumentScan]:
    """The most recent document scan of each verification, if it has one."""
    if not verification_ids:
        return {}
    scans = db.scalars(
        select(DocumentScan)
        .where(DocumentScan.verification_id.in_(verification_ids))
        .order_by(DocumentScan.verification_id, DocumentScan.created_at.desc())
    )
    latest: dict[int, DocumentScan] = {}
    for scan in scans:
        latest.setdefault(scan.verification_id, scan)
    return latest


@router.get("", response_model=ConsoleVerificationList)
def list_verifications(
    status_: str | None = Query(default=None, alias="status", description="One of " + ", ".join(_STATUSES)),
    client_id: int | None = Query(default=None, description="Only this client account's verifications"),
    q: str | None = Query(default=None, max_length=120, description="Reference, applicant name, email or customer id"),
    created_from: date | None = Query(default=None, description="Created on or after this day (UTC)"),
    created_to: date | None = Query(default=None, description="Created on or before this day (UTC)"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    _: Account = Depends(console_product("kyc")),
    db: Session = Depends(get_session),
) -> ConsoleVerificationList:
    """Every client's verifications, newest first."""
    if status_ is not None and status_ not in _STATUSES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Status must be one of " + ", ".join(_STATUSES))

    filters = []
    if client_id is not None:
        filters.append(Verification.account_id == client_id)
    if created_from is not None:
        filters.append(Verification.created_at >= datetime.combine(created_from, time.min, tzinfo=timezone.utc))
    if created_to is not None:
        filters.append(Verification.created_at < datetime.combine(created_to + timedelta(days=1), time.min, tzinfo=timezone.utc))
    if q and q.strip():
        like = f"%{q.strip()}%"
        filters.append(
            or_(
                Verification.reference.ilike(like),
                Applicant.customer_id.ilike(like),
                Applicant.email.ilike(like),
                func.concat_ws(" ", Applicant.first_name, Applicant.last_name).ilike(like),
                Verification.claimed["email"].astext.ilike(like),
                func.concat_ws(" ", Verification.claimed["first_name"].astext, Verification.claimed["last_name"].astext).ilike(like),
            )
        )

    base = select(Verification).outerjoin(Applicant, Verification.applicant_id == Applicant.id).where(*filters)

    # Counts per status under every filter but the status itself.
    counted = db.execute(
        select(_EFFECTIVE_STATUS.label("s"), func.count())
        .select_from(Verification)
        .outerjoin(Applicant, Verification.applicant_id == Applicant.id)
        .where(*filters)
        .group_by("s")
    ).all()
    status_counts = {s: 0 for s in _STATUSES} | {row[0]: row[1] for row in counted}

    listed = base.where(_EFFECTIVE_STATUS == status_) if status_ else base
    total = db.scalar(select(func.count()).select_from(listed.subquery())) or 0
    rows = db.scalars(
        listed.options(selectinload(Verification.applicant), selectinload(Verification.account))
        .order_by(Verification.created_at.desc(), Verification.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    scans = _latest_scans(db, [v.id for v in rows])
    return ConsoleVerificationList(
        items=[_summary(v, scans.get(v.id)) for v in rows],
        total=total,
        page=page,
        page_size=page_size,
        status_counts=status_counts,
    )


@router.get("/{verification_id}", response_model=ConsoleVerificationDetail)
def read_verification(
    verification_id: int,
    _: Account = Depends(console_product("kyc")),
    db: Session = Depends(get_session),
) -> ConsoleVerificationDetail:
    """One verification in full: its steps, their checks and what was read."""
    verification = db.get(Verification, verification_id)
    if verification is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No verification with that id")
    # The same as the client API does when a verification is looked at.
    verification = service.expire_if_due(db, verification)

    latest: dict = {}
    for step in verification.steps:
        current = latest.get(step.step)
        if current is None or step.attempt > current.attempt:
            latest[step.step] = step
    order = verification.services
    steps = sorted(latest.values(), key=lambda s: order.index(s.step.value) if s.step.value in order else 99)

    scan = _latest_scans(db, [verification.id]).get(verification.id)
    files = db.scalars(
        select(VerificationFile.kind).where(VerificationFile.verification_id == verification.id).distinct()
    ).all()

    summary = _summary(verification, scan)
    return ConsoleVerificationDetail(
        **summary.model_dump(),
        language=verification.language,
        warnings=verification.warnings,
        started_at=verification.started_at,
        steps=[
            ConsoleStep(
                step=step.step.value,
                attempt=step.attempt,
                status=step.status.value,
                reasons=step.reasons,
                warnings=step.warnings,
                checks=[
                    ConsoleCheck(
                        name=check.get("name", ""),
                        status=str(check.get("status", "")),
                        reasons=check.get("reasons") or [],
                        warnings=check.get("warnings") or [],
                    )
                    for check in (step.result or {}).get("checks", [])
                    if isinstance(check, dict)
                ],
                match_score=(step.result or {}).get("match_score"),
                match_threshold=(step.result or {}).get("threshold"),
                started_at=step.started_at,
                completed_at=step.completed_at,
            )
            for step in steps
        ],
        document=(
            ConsoleDocument(
                document_type=scan.document_type.value,
                decision=scan.decision.value,
                full_name=scan.full_name,
                date_of_birth=scan.date_of_birth,
                document_number_last4=scan.document_number[-4:] if scan.document_number else None,
                issuing_country=scan.issuing_country,
                nationality=scan.nationality,
                expiry_date=scan.expiry_date,
            )
            if scan
            else None
        ),
        files=sorted(kind.value for kind in files),
    )
