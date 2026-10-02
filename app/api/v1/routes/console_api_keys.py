"""Every client's API keys, for Pangolin staff in the admin console.

Only what the owner sees in their own list: the name, the prefix and the dates.
Never the key, which nobody holds after it is shown once, and never its hash.
Listing writes nothing.
"""

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import String, case, func, literal, or_, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import console_product
from app.db.models import Account, ApiKey
from app.db.session import get_session
from app.schemas.console import ConsoleApiKeyList, ConsoleApiKeyRow, ConsoleClient

router = APIRouter(prefix="/console/api-keys", tags=["admin console"])

_STATES = ("active", "expiring", "expired", "revoked")
# A key expiring within this many days is flagged, so its owner can be told in time.
EXPIRING_DAYS = 30

_STATE = case(
    (ApiKey.revoked_at.is_not(None), literal("revoked")),
    (ApiKey.expires_at <= func.now(), literal("expired")),
    (ApiKey.expires_at <= func.now() + timedelta(days=EXPIRING_DAYS), literal("expiring")),
    else_=literal("active"),
).cast(String)


@router.get("", response_model=ConsoleApiKeyList)
def list_api_keys(
    q: str | None = Query(default=None, max_length=120, description="Key name, prefix, or the client's name or email"),
    state: str | None = Query(default=None, description="active, expiring, expired or revoked"),
    client_id: int | None = Query(default=None, description="Only this client account's keys"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    _: Account = Depends(console_product("kyc")),
    db: Session = Depends(get_session),
) -> ConsoleApiKeyList:
    """Every client's keys, newest first."""
    if state is not None and state not in _STATES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "State must be one of " + ", ".join(_STATES))

    filters = []
    if client_id is not None:
        filters.append(ApiKey.account_id == client_id)
    if q and q.strip():
        text = q.strip()
        # A pasted key or a displayed "kyc_<prefix>_…" finds its key by the prefix.
        if text.startswith("kyc_") and text.count("_") >= 2:
            text = text.split("_")[1]
        like = f"%{text}%"
        filters.append(
            or_(
                ApiKey.name.ilike(like),
                ApiKey.prefix.ilike(like),
                Account.email.ilike(like),
                func.concat_ws(" ", Account.first_name, Account.last_name).ilike(like),
            )
        )

    joined = select(ApiKey).join(Account, ApiKey.account_id == Account.id).where(*filters)
    counted = db.execute(
        select(_STATE.label("s"), func.count())
        .select_from(ApiKey)
        .join(Account, ApiKey.account_id == Account.id)
        .where(*filters)
        .group_by("s")
    ).all()
    state_counts = {s: 0 for s in _STATES} | {row[0]: row[1] for row in counted}

    listed = joined.where(_STATE == state) if state else joined
    total = db.scalar(select(func.count()).select_from(listed.subquery())) or 0
    rows = db.execute(
        listed.add_columns(_STATE.label("state"))
        .options(selectinload(ApiKey.account))
        .order_by(ApiKey.created_at.desc(), ApiKey.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    return ConsoleApiKeyList(
        items=[
            ConsoleApiKeyRow(
                id=str(key.id),
                name=key.name,
                prefix=key.prefix,
                created_at=key.created_at,
                expires_at=key.expires_at,
                last_used_at=key.last_used_at,
                revoked_at=key.revoked_at,
                state=key_state,
                client=ConsoleClient(
                    id=str(key.account.id),
                    name=f"{key.account.first_name} {key.account.last_name}".strip(),
                    email=key.account.email,
                ),
            )
            for key, key_state in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
        state_counts=state_counts,
    )
