"""API keys: the credentials a system uses to call this service.

A key belongs to an account and is shown once, when it is made. What is kept is
a prefix, so a key can be found and recognised, and a hash, so a stolen copy of
the table is not a set of working credentials.

Every key expires. A key with no end date is a key nobody ever rotates, and the
one that leaks is always the oldest one.
"""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_account
from app.core import security
from app.db.models import Account, ApiKey
from app.db.session import get_session
from app.schemas.auth import ApiKeyResponse, CreateApiKeyRequest, CreatedApiKeyResponse

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
    db.commit()
    db.refresh(row)
    return CreatedApiKeyResponse(key=key, **_as_response(row))


@router.get("", response_model=list[ApiKeyResponse])
def list_keys(
    account: Account = Depends(current_account),
    db: Session = Depends(get_session),
) -> list[ApiKeyResponse]:
    """Every key on this account, including the spent ones, so an owner can see
    what exists and what it has been doing."""
    keys = db.scalars(
        select(ApiKey).where(ApiKey.account_id == account.id).order_by(ApiKey.created_at.desc())
    ).all()
    return [ApiKeyResponse(**_as_response(key)) for key in keys]


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
        db.commit()
        db.refresh(key)
    return ApiKeyResponse(**_as_response(key))



