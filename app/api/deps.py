"""Who is calling, and on whose behalf.

Three ways in, for three different callers:

  * **A person at the dashboard** presents a session token from signing in.
    `current_account` turns it into their account.
  * **A system calling the API** presents an API key belonging to an account.
    `account_from_api_key` turns it into the same thing.
  * **Pangolin staff in the admin console** present a console token.
    `current_console_account` turns it into their account, and only if it
    still has a console role.

All three end at an Account, so anything they reach can be written the same way.
"""

from datetime import UTC, datetime

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import security
from app.db.models import Account, ApiKey
from app.db.session import get_session

_bearer = HTTPBearer(auto_error=False)
_API_KEY_HEADER = "X-API-Key"


def current_account(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_session),
) -> Account:
    """The account behind a dashboard session token."""
    if credentials is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Sign in first",
            headers={"WWW-Authenticate": "Bearer"},
        )
    claims = security.read_access_token(credentials.credentials)
    if claims is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "That session has expired. Sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    account = db.get(Account, _as_id(claims.get("sub")))
    if account is None or not account.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "That account is no longer active")
    return account


def current_console_account(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_session),
) -> Account:
    """The staff account behind an admin console token.

    The role is read from the account, not trusted from the token, so taking it
    away (or suspending the account) locks the person out on their next request.
    """
    if credentials is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Sign in first",
            headers={"WWW-Authenticate": "Bearer"},
        )
    claims = security.read_console_token(credentials.credentials)
    if claims is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "That session has expired. Sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    account = db.get(Account, _as_id(claims.get("sub")))
    if account is None or not account.is_active or not account.console_role:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "This account cannot use the admin console")
    return account


def account_from_api_key(
    x_api_key: str | None = Header(default=None, alias=_API_KEY_HEADER),
    db: Session = Depends(get_session),
) -> Account:
    """The account a presented API key belongs to.

    A key is found by its prefix and then compared by hash, in constant time, so
    a wrong key cannot be discovered one character at a time. Expired, revoked
    and suspended all give the same answer as wrong: telling a caller *why* their
    key failed tells them which keys exist.
    """
    if not x_api_key:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            f"Missing {_API_KEY_HEADER}",
            headers={"WWW-Authenticate": _API_KEY_HEADER},
        )

    refused = HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key")
    prefix = security.split_api_key(x_api_key)
    if prefix is None:
        raise refused

    key = db.scalar(select(ApiKey).where(ApiKey.prefix == prefix))
    if key is None or not security.same_secret(security.fingerprint(x_api_key), key.secret_hash):
        raise refused

    now = datetime.now(UTC)
    if key.revoked_at is not None or _expired(key.expires_at, now):
        raise refused

    account = db.get(Account, key.account_id)
    if account is None or not account.is_active:
        raise refused

    # Written on every call, so a key nobody uses can be found and removed. It is
    # a timestamp on a row the caller already proved they hold: no extra risk.
    key.last_used_at = now
    db.commit()
    return account


def _expired(when: datetime, now: datetime) -> bool:
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when < now


def _as_id(value) -> int | None:
    """The account id out of a token's subject, or None if it is not one."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
