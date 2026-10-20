"""Every client's API keys, for staff in the admin console.

Only what the owner sees in their own list: the name, the prefix and the dates.
Never the key, which nobody holds after it is shown once, and never its hash.
Listing writes nothing.

Console owners and admins can also stop a key:

  * **Suspend** stops it for now, and **resume** lets it work again. For a key
    that is misbehaving or under investigation.
  * **Expire** ends it for good, by moving its expiry to now. The client makes a
    new key. For a key that has leaked.

Each is recorded in audit_events with who did it and why, and the latest one is
shown with the key.
"""

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import String, case, func, literal, or_, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import console_product, console_role
from app.db.models import Account, ApiKey, AuditEvent
from app.db.session import get_session
from app.schemas.console import (
    ConsoleApiKeyAction,
    ConsoleApiKeyActionRequest,
    ConsoleApiKeyList,
    ConsoleApiKeyRow,
    ConsoleClient,
)

router = APIRouter(prefix="/console/api-keys", tags=["admin console"])

_STATES = ("active", "expiring", "suspended", "expired", "revoked")
# A key expiring within this many days is flagged, so its owner can be told in time.
EXPIRING_DAYS = 30
# The console roles that may suspend, resume or expire a key.
KEY_ADMINS = ("owner", "admin")

# Revoked and expired are final, so they win over suspended.
_STATE = case(
    (ApiKey.revoked_at.is_not(None), literal("revoked")),
    (ApiKey.expires_at <= func.now(), literal("expired")),
    (ApiKey.suspended_at.is_not(None), literal("suspended")),
    (ApiKey.expires_at <= func.now() + timedelta(days=EXPIRING_DAYS), literal("expiring")),
    else_=literal("active"),
).cast(String)

_ACTIONS = ("suspended", "resumed", "expired")


def _last_actions(db: Session, key_ids: list[int]) -> dict[int, ConsoleApiKeyAction]:
    """The most recent console action on each of these keys, if any."""
    if not key_ids:
        return {}
    events = db.scalars(
        select(AuditEvent)
        .where(
            AuditEvent.event_type.in_([f"api_key.{a}" for a in _ACTIONS]),
            AuditEvent.payload["api_key_id"].astext.in_([str(i) for i in key_ids]),
        )
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
    )
    latest: dict[int, ConsoleApiKeyAction] = {}
    for event in events:
        key_id = int(event.payload["api_key_id"])
        latest.setdefault(
            key_id,
            ConsoleApiKeyAction(
                action=event.event_type.removeprefix("api_key."),
                by=event.payload.get("by") or event.actor,
                reason=event.payload.get("reason"),
                at=event.created_at,
            ),
        )
    return latest


def _row(key: ApiKey, state: str, last_action: ConsoleApiKeyAction | None) -> ConsoleApiKeyRow:
    return ConsoleApiKeyRow(
        id=str(key.id),
        name=key.name,
        prefix=key.prefix,
        created_at=key.created_at,
        expires_at=key.expires_at,
        last_used_at=key.last_used_at,
        revoked_at=key.revoked_at,
        suspended_at=key.suspended_at,
        deleted_at=key.deleted_at,
        state=state,
        client=ConsoleClient(
            id=str(key.account.id),
            name=f"{key.account.first_name} {key.account.last_name}".strip(),
            email=key.account.email,
        ),
        last_action=last_action,
    )


@router.get("", response_model=ConsoleApiKeyList)
def list_api_keys(
    q: str | None = Query(default=None, max_length=120, description="Key name, prefix, or the client's name or email"),
    state: str | None = Query(default=None, description="active, expiring, suspended, expired or revoked"),
    client_id: int | None = Query(default=None, description="Only this client account's keys"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    _: Account = Depends(console_product("kyc")),
    db: Session = Depends(get_session),
) -> ConsoleApiKeyList:
    """Every client's keys, newest first."""
    return api_key_list(db, q=q, state=state, client_id=client_id, page=page, page_size=page_size)


def api_key_list(
    db: Session,
    *,
    q: str | None,
    state: str | None,
    client_id: int | None,
    page: int,
    page_size: int,
    include_deleted: bool = True,
) -> ConsoleApiKeyList:
    """Keys newest first, of every client or only of `client_id`. Shared with the
    client's own list, which always passes its account and leaves out the keys it
    deleted."""
    if state is not None and state not in _STATES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "State must be one of " + ", ".join(_STATES))

    filters = []
    if client_id is not None:
        filters.append(ApiKey.account_id == client_id)
    if not include_deleted:
        filters.append(ApiKey.deleted_at.is_(None))
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
    actions = _last_actions(db, [key.id for key, _ in rows])

    return ConsoleApiKeyList(
        items=[_row(key, key_state, actions.get(key.id)) for key, key_state in rows],
        total=total,
        page=page,
        page_size=page_size,
        state_counts=state_counts,
    )


def _key(db: Session, key_id: int) -> ApiKey:
    key = db.get(ApiKey, key_id)
    if key is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No key with that id")
    return key


def _state(db: Session, key: ApiKey) -> str:
    return db.scalar(select(_STATE).where(ApiKey.id == key.id))


def _record(db: Session, key: ApiKey, action: str, staff: Account, reason: str | None) -> None:
    db.add(
        AuditEvent(
            event_type=f"api_key.{action}",
            actor=str(staff.id),
            payload={
                "api_key_id": str(key.id),
                "prefix": key.prefix,
                "account_id": str(key.account_id),
                "by": staff.email,
                "reason": reason,
            },
        )
    )


def _respond(db: Session, key: ApiKey) -> ConsoleApiKeyRow:
    db.refresh(key)
    return _row(key, _state(db, key), _last_actions(db, [key.id]).get(key.id))


def _reason(body: ConsoleApiKeyActionRequest | None) -> str | None:
    if body is None or body.reason is None:
        return None
    return body.reason.strip() or None


@router.post("/{key_id}/suspend", response_model=ConsoleApiKeyRow)
def suspend_api_key(
    key_id: int,
    body: ConsoleApiKeyActionRequest | None = None,
    staff: Account = Depends(console_role("kyc", KEY_ADMINS)),
    db: Session = Depends(get_session),
) -> ConsoleApiKeyRow:
    """Stop a key working until it is resumed. Calls made with it are refused with
    the same answer as a wrong key."""
    key = _key(db, key_id)
    current = _state(db, key)
    if current in ("revoked", "expired"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"This key is already {current}, so it cannot be suspended")
    if key.suspended_at is None:
        key.suspended_at = func.now()
        _record(db, key, "suspended", staff, _reason(body))
        db.commit()
    return _respond(db, key)


@router.post("/{key_id}/resume", response_model=ConsoleApiKeyRow)
def resume_api_key(
    key_id: int,
    body: ConsoleApiKeyActionRequest | None = None,
    staff: Account = Depends(console_role("kyc", KEY_ADMINS)),
    db: Session = Depends(get_session),
) -> ConsoleApiKeyRow:
    """Let a suspended key work again, unless it has since expired or been revoked."""
    key = _key(db, key_id)
    if key.suspended_at is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This key is not suspended")
    current = _state(db, key)
    if current in ("revoked", "expired"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"This key is {current}, so resuming it would not make it work")
    key.suspended_at = None
    _record(db, key, "resumed", staff, _reason(body))
    db.commit()
    return _respond(db, key)


@router.post("/{key_id}/expire", response_model=ConsoleApiKeyRow)
def expire_api_key(
    key_id: int,
    body: ConsoleApiKeyActionRequest | None = None,
    staff: Account = Depends(console_role("kyc", KEY_ADMINS)),
    db: Session = Depends(get_session),
) -> ConsoleApiKeyRow:
    """End a key for good by moving its expiry to now. The client makes a new one."""
    key = _key(db, key_id)
    current = _state(db, key)
    if current in ("revoked", "expired"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"This key is already {current}")
    # The database's clock, the one the state above is worked out with.
    key.expires_at = func.now()
    _record(db, key, "expired", staff, _reason(body))
    db.commit()
    return _respond(db, key)
