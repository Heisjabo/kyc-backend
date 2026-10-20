"""Every applicant, for staff in the admin console.

An applicant is a person a client has asked us to verify, kept across their
verifications under one customer id. Each is listed with their most recent
verification, by the status a client would see now, so a lapsed session shows as
expired. Listing writes nothing.
"""

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import console_product
from app.api.v1.routes.console_clients import _day_start
from app.api.v1.routes.console_verifications import _EFFECTIVE_STATUS, _latest_scans, _summary
from app.db.models import Account, Applicant, Verification, VerificationStatus
from app.db.session import get_session
from app.schemas.console import (
    ConsoleApplicantDetail,
    ConsoleApplicantList,
    ConsoleApplicantSummary,
    ConsoleClient,
)
router = APIRouter(prefix="/console/applicants", tags=["admin console"])

_STATUSES = [s.value for s in VerificationStatus]


def _latest():
    """Per applicant: how many verifications, and the newest one's status and time."""
    ranked = (
        select(
            Verification.applicant_id.label("applicant_id"),
            _EFFECTIVE_STATUS.label("status"),
            Verification.created_at.label("created_at"),
            func.row_number()
            .over(partition_by=Verification.applicant_id, order_by=(Verification.created_at.desc(), Verification.id.desc()))
            .label("rank"),
            func.count().over(partition_by=Verification.applicant_id).label("verifications"),
        )
        .where(Verification.applicant_id.is_not(None))
        .subquery()
    )
    return select(ranked).where(ranked.c.rank == 1).subquery()


def _name(applicant: Applicant) -> str | None:
    return " ".join(part for part in (applicant.first_name, applicant.last_name) if part) or None


def _summary_fields(applicant: Applicant, row) -> dict:
    account = applicant.account
    return {
        "id": str(applicant.id),
        "customer_id": applicant.customer_id,
        "client_customer_id": applicant.client_customer_id,
        "name": _name(applicant),
        "email": applicant.email,
        "phone": applicant.phone,
        "country": applicant.country,
        "client": ConsoleClient(
            id=str(account.id), name=f"{account.first_name} {account.last_name}".strip(), email=account.email
        ),
        "created_at": applicant.created_at,
        "verifications": row.verifications or 0,
        "latest_status": row.status,
        "latest_at": row.created_at,
    }


@router.get("", response_model=ConsoleApplicantList)
def list_applicants(
    q: str | None = Query(default=None, max_length=120, description="Name, email, phone, or either customer id"),
    status_: str | None = Query(default=None, alias="status", description="Latest verification status, or none"),
    client_id: int | None = Query(default=None, description="Only this client account's applicants"),
    country: str | None = Query(default=None, max_length=3),
    created_from: date | None = Query(default=None, description="First seen on or after this day (UTC)"),
    created_to: date | None = Query(default=None, description="First seen on or before this day (UTC)"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    _: Account = Depends(console_product("kyc")),
    db: Session = Depends(get_session),
) -> ConsoleApplicantList:
    """Every applicant, most recent activity first."""
    if status_ is not None and status_ not in (*_STATUSES, "none"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Status must be one of " + ", ".join(_STATUSES) + " or none")

    latest = _latest()
    filters = []
    if client_id is not None:
        filters.append(Applicant.account_id == client_id)
    if country:
        filters.append(Applicant.country == country.upper())
    if created_from is not None:
        filters.append(Applicant.created_at >= _day_start(created_from))
    if created_to is not None:
        filters.append(Applicant.created_at < _day_start(created_to + timedelta(days=1)))
    if q and q.strip():
        like = f"%{q.strip()}%"
        filters.append(
            or_(
                Applicant.customer_id.ilike(like),
                Applicant.client_customer_id.ilike(like),
                Applicant.email.ilike(like),
                func.concat_ws(" ", Applicant.first_name, Applicant.last_name).ilike(like),
                func.replace(Applicant.phone, " ", "").ilike(like.replace(" ", "")),
            )
        )

    def joined(*columns):
        return select(*columns).select_from(Applicant).outerjoin(latest, latest.c.applicant_id == Applicant.id).where(*filters)

    status_key = func.coalesce(latest.c.status, "none")
    counted = db.execute(joined(status_key.label("s"), func.count()).group_by("s")).all()
    status_counts = {s: 0 for s in (*_STATUSES, "none")} | {row[0]: row[1] for row in counted}

    def narrowed(query):
        return query.where(status_key == status_) if status_ else query

    total = db.scalar(narrowed(joined(func.count()))) or 0
    rows = db.execute(
        narrowed(joined(Applicant, latest.c.status, latest.c.created_at, latest.c.verifications))
        .options(selectinload(Applicant.account))
        .order_by(func.coalesce(latest.c.created_at, Applicant.created_at).desc(), Applicant.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    countries = db.scalars(select(Applicant.country).where(Applicant.country.is_not(None)).distinct().order_by(Applicant.country)).all()

    return ConsoleApplicantList(
        items=[ConsoleApplicantSummary(**_summary_fields(row.Applicant, row)) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
        status_counts=status_counts,
        countries=list(countries),
    )


@router.get("/{applicant_id}", response_model=ConsoleApplicantDetail)
def read_applicant(
    applicant_id: int,
    _: Account = Depends(console_product("kyc")),
    db: Session = Depends(get_session),
) -> ConsoleApplicantDetail:
    """One applicant with every verification they have had, newest first."""
    latest = _latest()
    row = db.execute(
        select(Applicant, latest.c.status, latest.c.created_at, latest.c.verifications)
        .outerjoin(latest, latest.c.applicant_id == Applicant.id)
        .where(Applicant.id == applicant_id)
        .options(selectinload(Applicant.account))
    ).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No applicant with that id")
    applicant: Applicant = row.Applicant

    history = db.scalars(
        select(Verification)
        .where(Verification.applicant_id == applicant.id)
        .options(selectinload(Verification.applicant), selectinload(Verification.account))
        .order_by(Verification.created_at.desc(), Verification.id.desc())
    ).all()
    scans = _latest_scans(db, [v.id for v in history])

    return ConsoleApplicantDetail(
        **_summary_fields(applicant, row),
        first_name=applicant.first_name,
        last_name=applicant.last_name,
        date_of_birth=applicant.date_of_birth,
        history=[_summary(v, scans.get(v.id)) for v in history],
    )
