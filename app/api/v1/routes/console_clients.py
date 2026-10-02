"""Every client account, for Pangolin staff in the admin console.

A client is an account calling the API. Console staff hold accounts too, so an
account counts as a client unless it is a staff account that has never opened a
verification; a staff member who also tested the API still shows up, with the
verifications they made.

The list filters the way the Tuesday Markets admin filters its users: a search,
the account status, the last sign-in, the country and when the account was made.
Listing writes nothing.
"""

from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import console_product
from app.api.v1.routes.console_verifications import _EFFECTIVE_STATUS, _latest_scans, _summary
from app.db.models import Account, ApiKey, Applicant, Verification, VerificationStatus
from app.db.session import get_session
from app.schemas.console import (
    ConsoleApiKey,
    ConsoleClientDetail,
    ConsoleClientList,
    ConsoleClientOption,
    ConsoleClientOverview,
    ConsoleClientSummary,
)

router = APIRouter(prefix="/console/clients", tags=["admin console"])

_STATUSES = ("active", "suspended")
_LAST_SIGN_IN = {"today": 1, "week": 7, "month": 30}

_IS_CLIENT = or_(
    Account.console_role.is_(None),
    exists(select(Verification.id).where(Verification.account_id == Account.id)),
)


def _name(account: Account) -> str:
    return f"{account.first_name} {account.last_name}".strip()


def _verification_stats():
    """Per account: how many verifications, by the status a client would see now."""
    effective = _EFFECTIVE_STATUS
    return (
        select(
            Verification.account_id.label("account_id"),
            func.count().label("verifications"),
            func.count().filter(effective == VerificationStatus.passed.value).label("passed"),
            func.count().filter(effective == VerificationStatus.failed.value).label("failed"),
            func.count().filter(effective == VerificationStatus.needs_review.value).label("needs_review"),
            func.count()
            .filter(effective.in_([VerificationStatus.pending.value, VerificationStatus.in_progress.value]))
            .label("in_progress"),
            func.max(Verification.created_at).label("last_verification_at"),
        )
        .group_by(Verification.account_id)
        .subquery()
    )


def _active_keys():
    """Per account: keys that are neither revoked nor expired."""
    return (
        select(ApiKey.account_id.label("account_id"), func.count().label("active_keys"))
        .where(ApiKey.revoked_at.is_(None), ApiKey.expires_at > func.now())
        .group_by(ApiKey.account_id)
        .subquery()
    )


def _summary_fields(account: Account, row) -> dict:
    return {
        "id": str(account.id),
        "name": _name(account),
        "email": account.email,
        "phone": f"{account.country_code} {account.phone}".strip(),
        "country": account.country,
        "status": "active" if account.is_active else "suspended",
        "created_at": account.created_at,
        "last_login_at": account.last_login_at,
        "verifications": row.verifications or 0,
        "passed": row.passed or 0,
        "failed": row.failed or 0,
        "needs_review": row.needs_review or 0,
        "last_verification_at": row.last_verification_at,
        "active_keys": row.active_keys or 0,
    }


def _day_start(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


@router.get("/options", response_model=list[ConsoleClientOption])
def client_options(
    _: Account = Depends(console_product("kyc")),
    db: Session = Depends(get_session),
) -> list[ConsoleClientOption]:
    """Every client by name, for a filter on another page."""
    accounts = db.scalars(select(Account).where(_IS_CLIENT).order_by(func.lower(Account.first_name), Account.id))
    return [ConsoleClientOption(id=str(a.id), name=_name(a), email=a.email) for a in accounts]


@router.get("", response_model=ConsoleClientList)
def list_clients(
    q: str | None = Query(default=None, max_length=120, description="Name, email or phone"),
    status_: str | None = Query(default=None, alias="status", description="active or suspended"),
    last_sign_in: str | None = Query(default=None, description="today, week, month or never"),
    country: str | None = Query(default=None, max_length=80),
    joined_from: date | None = Query(default=None, description="Made on or after this day (UTC)"),
    joined_to: date | None = Query(default=None, description="Made on or before this day (UTC)"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    _: Account = Depends(console_product("kyc")),
    db: Session = Depends(get_session),
) -> ConsoleClientList:
    """Every client account, newest first, with what it has done."""
    if status_ is not None and status_ not in _STATUSES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Status must be active or suspended")
    if last_sign_in is not None and last_sign_in not in (*_LAST_SIGN_IN, "never"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Last sign-in must be today, week, month or never")

    filters = [_IS_CLIENT]
    if q and q.strip():
        like = f"%{q.strip()}%"
        filters.append(
            or_(
                Account.email.ilike(like),
                func.concat_ws(" ", Account.first_name, Account.last_name).ilike(like),
                Account.phone.ilike(like),
                func.concat(Account.country_code, Account.phone).ilike(like.replace(" ", "")),
            )
        )
    if status_ is not None:
        filters.append(Account.is_active.is_(status_ == "active"))
    if last_sign_in == "never":
        filters.append(Account.last_login_at.is_(None))
    elif last_sign_in is not None:
        filters.append(Account.last_login_at >= func.now() - timedelta(days=_LAST_SIGN_IN[last_sign_in]))
    if country:
        filters.append(Account.country == country)
    if joined_from is not None:
        filters.append(Account.created_at >= _day_start(joined_from))
    if joined_to is not None:
        filters.append(Account.created_at < _day_start(joined_to + timedelta(days=1)))

    stats = _verification_stats()
    keys = _active_keys()
    total = db.scalar(select(func.count()).select_from(Account).where(*filters)) or 0
    rows = db.execute(
        select(Account, stats, keys.c.active_keys)
        .outerjoin(stats, stats.c.account_id == Account.id)
        .outerjoin(keys, keys.c.account_id == Account.id)
        .where(*filters)
        .order_by(Account.created_at.desc(), Account.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    clients = db.execute(
        select(
            func.count(),
            func.count().filter(Account.is_active.is_(True)),
            func.count().filter(Account.is_active.is_(False)),
        ).where(_IS_CLIENT)
    ).one()
    client_ids = select(Account.id).where(_IS_CLIENT)
    overview = ConsoleClientOverview(
        clients=clients[0],
        active=clients[1],
        suspended=clients[2],
        active_keys=db.scalar(
            select(func.count())
            .select_from(ApiKey)
            .where(ApiKey.account_id.in_(client_ids), ApiKey.revoked_at.is_(None), ApiKey.expires_at > func.now())
        )
        or 0,
        verifications=db.scalar(select(func.count()).select_from(Verification).where(Verification.account_id.in_(client_ids))) or 0,
    )
    countries = db.scalars(select(Account.country).where(_IS_CLIENT).distinct().order_by(Account.country)).all()

    return ConsoleClientList(
        items=[ConsoleClientSummary(**_summary_fields(row.Account, row)) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
        overview=overview,
        countries=[c for c in countries if c],
    )


@router.get("/{client_id}", response_model=ConsoleClientDetail)
def read_client(
    client_id: int,
    _: Account = Depends(console_product("kyc")),
    db: Session = Depends(get_session),
) -> ConsoleClientDetail:
    """One client in full: its owner, its keys and its latest verifications."""
    stats = _verification_stats()
    keys = _active_keys()
    row = db.execute(
        select(Account, stats, keys.c.active_keys)
        .outerjoin(stats, stats.c.account_id == Account.id)
        .outerjoin(keys, keys.c.account_id == Account.id)
        .where(Account.id == client_id, _IS_CLIENT)
    ).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No client with that id")
    account: Account = row.Account

    api_keys = db.scalars(select(ApiKey).where(ApiKey.account_id == account.id).order_by(ApiKey.created_at.desc())).all()
    recent = db.scalars(
        select(Verification)
        .where(Verification.account_id == account.id)
        .options(selectinload(Verification.applicant), selectinload(Verification.account))
        .order_by(Verification.created_at.desc(), Verification.id.desc())
        .limit(5)
    ).all()
    scans = _latest_scans(db, [v.id for v in recent])
    applicants = db.scalar(select(func.count()).select_from(Applicant).where(Applicant.account_id == account.id)) or 0

    return ConsoleClientDetail(
        **_summary_fields(account, row),
        first_name=account.first_name,
        last_name=account.last_name,
        country_code=account.country_code,
        city=account.city,
        address=account.address,
        in_progress=row.in_progress or 0,
        applicants=applicants,
        api_keys=[
            ConsoleApiKey(
                name=k.name,
                prefix=k.prefix,
                created_at=k.created_at,
                expires_at=k.expires_at,
                last_used_at=k.last_used_at,
                revoked_at=k.revoked_at,
            )
            for k in api_keys
        ],
        recent_verifications=[_summary(v, scans.get(v.id)) for v in recent],
    )
