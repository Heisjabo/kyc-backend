"""Every account, for staff in the admin console.

A client is an account calling the API; staff are accounts with a console role.
The list shows both, with each account's role, and filters by it. The client
filter on other pages (`/options`) still lists clients only: an account counts
as a client unless it is a staff account that has never opened a verification.

Owners and admins can also create an account, client or staff, and change an
account's console role. Only an owner gives or takes the owner role, nobody
changes their own, and the last active owner keeps it. Both are kept in
audit_events (account.created, account.access_updated).

The list filters by a search,
the account status, the last sign-in, the country, the billing plan and when the
account was made.
Listing writes nothing.
"""

from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import console_product
from app.api.v1.routes.console_team import _active_owners, _check_products, _check_role, _record, team_manager
from app.core import security
from app.core.plans import PLANS
from app.api.v1.routes.console_verifications import _EFFECTIVE_STATUS, _latest_scans, _summary
from app.db.models import CONSOLE_ROLES, Account, ApiKey, Applicant, Verification, VerificationStatus
from app.db.session import get_session
from app.schemas.console import (
    ConsoleAccountAccess,
    ConsoleAccountCreate,
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
        .where(ApiKey.revoked_at.is_(None), ApiKey.suspended_at.is_(None), ApiKey.expires_at > func.now())
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
        "role": account.console_role,
        "products": list(account.console_products or []),
        "plan": account.plan,
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
    role: str | None = Query(default=None, description="client, staff, or a console role"),
    plan: str | None = Query(default=None, description="free, payg or enterprise"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    _: Account = Depends(console_product("kyc")),
    db: Session = Depends(get_session),
) -> ConsoleClientList:
    """Every account, newest first, with its role and what it has done."""
    if status_ is not None and status_ not in _STATUSES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Status must be active or suspended")
    if last_sign_in is not None and last_sign_in not in (*_LAST_SIGN_IN, "never"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Last sign-in must be today, week, month or never")

    if role is not None and role not in ("client", "staff", *CONSOLE_ROLES):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Role must be client, staff or a console role")
    if plan is not None and plan not in PLANS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Plan must be one of: {', '.join(PLANS)}")

    filters = []
    if role == "client":
        filters.append(Account.console_role.is_(None))
    elif role == "staff":
        filters.append(Account.console_role.is_not(None))
    elif role is not None:
        filters.append(Account.console_role == role)
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
    if plan is not None:
        filters.append(Account.plan == plan)
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

    accounts = db.execute(
        select(
            func.count(),
            func.count().filter(Account.is_active.is_(True)),
            func.count().filter(Account.is_active.is_(False)),
            func.count().filter(Account.console_role.is_not(None)),
        ).select_from(Account)
    ).one()
    overview = ConsoleClientOverview(
        clients=accounts[0],
        active=accounts[1],
        suspended=accounts[2],
        staff=accounts[3],
        active_keys=db.scalar(
            select(func.count())
            .select_from(ApiKey)
            .where(ApiKey.revoked_at.is_(None), ApiKey.suspended_at.is_(None), ApiKey.expires_at > func.now())
        )
        or 0,
        verifications=db.scalar(select(func.count()).select_from(Verification)) or 0,
    )
    countries = db.scalars(select(Account.country).distinct().order_by(Account.country)).all()

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
    """One account in full: its owner, its keys and its latest verifications."""
    return _detail(db, client_id)


def _detail(db: Session, client_id: int) -> ConsoleClientDetail:
    stats = _verification_stats()
    keys = _active_keys()
    row = db.execute(
        select(Account, stats, keys.c.active_keys)
        .outerjoin(stats, stats.c.account_id == Account.id)
        .outerjoin(keys, keys.c.account_id == Account.id)
        .where(Account.id == client_id)
    ).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No account with that id")
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
                suspended_at=k.suspended_at,
            )
            for k in api_keys
        ],
        recent_verifications=[_summary(v, scans.get(v.id)) for v in recent],
    )


@router.post("", response_model=ConsoleClientDetail, status_code=status.HTTP_201_CREATED)
def create_account(
    body: ConsoleAccountCreate,
    request: Request,
    staff: Account = Depends(team_manager),
    db: Session = Depends(get_session),
) -> ConsoleClientDetail:
    """Create an account with the password given here, as a client or with a
    console role. The person signs in with it and can change it in Settings."""
    email = body.email.strip().lower()
    if db.scalar(select(Account).where(Account.email == email)) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists")
    role = _check_role(body.role, staff) if body.role else None
    products = _check_products(body.products) if role else []

    account = Account(
        email=email,
        password_hash=security.hash_password(body.password),
        first_name=body.first_name.strip(),
        last_name=body.last_name.strip(),
        phone=body.phone.strip(),
        country_code=body.country_code.strip(),
        country=body.country.strip(),
        date_of_birth=body.date_of_birth,
        console_role=role,
        console_products=products,
    )
    db.add(account)
    db.flush()
    _record(db, staff, "account.created", request, email=email, account_id=account.id, role=role, products=products)
    db.commit()
    return _detail(db, account.id)


@router.patch("/{client_id}/access", response_model=ConsoleClientDetail)
def update_access(
    client_id: int,
    body: ConsoleAccountAccess,
    request: Request,
    staff: Account = Depends(team_manager),
    db: Session = Depends(get_session),
) -> ConsoleClientDetail:
    """Change an account's console role and products, or make it a client account."""
    account = db.get(Account, client_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No account with that id")
    if account.id == staff.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You cannot change your own role")
    if account.console_role == "owner" and staff.console_role != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only an owner can change another owner")

    role = _check_role(body.role, staff) if body.role else None
    products = _check_products(body.products) if role else []
    if account.console_role == "owner" and role != "owner" and account.is_active and _active_owners(db) <= 1:
        raise HTTPException(status.HTTP_409_CONFLICT, "The console needs at least one active owner")

    before = {"role": account.console_role, "products": list(account.console_products or [])}
    account.console_role = role
    account.console_products = products
    after = {"role": role, "products": products}
    if after != before:
        _record(db, staff, "account.access_updated", request, email=account.email, account_id=account.id, before=before, after=after)
    db.commit()
    return _detail(db, account.id)
