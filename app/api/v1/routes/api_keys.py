"""API keys: the credentials a system uses to call this service.

A key belongs to an account and is shown once, when it is made. What is kept is
a prefix, so a key can be found and recognised, and a hash, so a stolen copy of
the table is not a set of working credentials.

Every key expires. A key with no end date is a key nobody ever rotates, and the
one that leaks is always the oldest one.
"""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import Caller, current_account, current_caller
from app.core import security
from app.db.models import Account, ApiKey, AuditEvent
from app.db.session import get_session
from app.schemas.auth import ApiKeyResponse, CreateApiKeyRequest, CreatedApiKeyResponse
from app.api.v1.routes.console_api_keys import api_key_list
from app.schemas.console import ConsoleApiKeyList

router = APIRouter(prefix="/api-keys", tags=["api keys"])


def _as_response(key: ApiKey) -> dict:
    now = datetime.now(UTC)
    expires = key.expires_at if key.expires_at.tzinfo else key.expires_at.replace(tzinfo=UTC)
    return {
        "id": str(key.id),
        "name": key.name,
        "prefix": key.prefix,
        "expires_at": key.expires_at,
        "created_at": key.created_at,
        "last_used_at": key.last_used_at,
        "revoked_at": key.revoked_at,
        "suspended_at": key.suspended_at,
        "is_valid": key.revoked_at is None and key.suspended_at is None and expires > now,
    }


def _record(db: Session, account: Account, action: str, key: ApiKey) -> None:
    """Kept in audit_events, so the account's API logs show who made or stopped which key."""
    db.add(
        AuditEvent(
            event_type=f"api_key.{action}",
            actor=str(account.id),
            payload={"api_key_id": str(key.id), "name": key.name, "prefix": key.prefix, "by": account.email},
        )
    )


@router.post("", response_model=CreatedApiKeyResponse, status_code=status.HTTP_201_CREATED)
def create_key(
    body: CreateApiKeyRequest,
    account: Account = Depends(current_account),
    db: Session = Depends(get_session),
) -> CreatedApiKeyResponse:
    """Make a key. This response is the only time the key itself is returned."""
    key, prefix, secret_hash = security.new_api_key()
    row = ApiKey(
        account_id=account.id,
        name=body.name.strip(),
        prefix=prefix,
        secret_hash=secret_hash,
        expires_at=datetime.now(UTC) + timedelta(days=body.expires_in_days),
    )
    db.add(row)
    db.flush()
    _record(db, account, "created", row)
    db.commit()
    db.refresh(row)
    return CreatedApiKeyResponse(key=key, **_as_response(row))


@router.get("", response_model=ConsoleApiKeyList)
def list_keys(
    q: str | None = Query(default=None, max_length=120, description="Key name or prefix"),
    state: str | None = Query(default=None, description="active, expiring, suspended, expired or revoked"),
    client_id: int | None = Query(default=None, description="Pangolin staff only: one client's keys"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    caller: Caller = Depends(current_caller),
    db: Session = Depends(get_session),
) -> ConsoleApiKeyList:
    """Keys newest first, including the spent ones, with a count per state. A
    client gets its own, so it can see what exists and what each has been doing;
    Pangolin staff get every client's."""
    keys = api_key_list(db, q=q, state=state, client_id=caller.scope_for(client_id), page=page, page_size=page_size)
    if not caller.staff:
        for row in keys.items:
            if row.last_action is not None:
                # Pangolin suspended or resumed it; which staff member did is not the client's.
                row.last_action.by = "Pangolin compliance"
    return keys


@router.delete("/{key_id}", response_model=ApiKeyResponse)
def revoke_key(
    key_id: str,
    account: Account = Depends(current_account),
    db: Session = Depends(get_session),
) -> ApiKeyResponse:
    """Stop a key working. The row stays: what it did should not disappear with it."""
    # Keys are named either by their prefix, which is what a person reads off a
    # list, or by their id.
    key = db.scalar(
        select(ApiKey).where(ApiKey.prefix == key_id, ApiKey.account_id == account.id)
    )
    if key is None and key_id.isdigit():
        key = db.scalar(
            select(ApiKey).where(ApiKey.id == int(key_id), ApiKey.account_id == account.id)
        )
    if key is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such key on this account")
    if key.revoked_at is None:
        key.revoked_at = datetime.now(UTC)
        _record(db, account, "revoked", key)
        db.commit()
        db.refresh(key)
    return ApiKeyResponse(**_as_response(key))



