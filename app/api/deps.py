"""Who is calling, and on whose behalf.

Three ways in, for three different callers:

  * **A person at the dashboard** presents a session token from signing in.
    `current_account` turns it into their account.
  * **A system calling the API** presents an API key belonging to an account.
    `account_from_api_key` turns it into the same thing.
  * **Staff in the admin console** present a console token.
    `current_console_account` turns it into their account, and only if it
    still has a console role.

All three end at an Account, so anything they reach can be written the same way.

Endpoints that every caller shares take `current_caller`, which accepts any of
the three and says how far the caller may see: a client, through the dashboard
or an API key, sees only its own account's data; staff with KYC in
their console products see every account's.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.request_audit import set_actor
from app.core import security
from app.core.config import settings
from app.db.models import Account, ApiKey
from app.db.session import get_session

_bearer = HTTPBearer(auto_error=False)
_API_KEY_HEADER = "X-API-Key"


def current_account(
    request: Request,
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
    _same_version(claims, account)
    set_actor(request, account.id, "dashboard")
    return account


def _same_version(claims: dict, account: Account) -> None:
    """A token from before the last password change or suspension is no longer good."""
    # Tokens issued before this existed carry no version and are left alone.
    if claims.get("ver") is not None and claims["ver"] != account.token_version:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "That session has ended. Sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def current_console_account(
    request: Request,
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
    _same_version(claims, account)
    set_actor(request, account.id, "console")
    return account


def console_product(product: str):
    """A dependency for console endpoints that belong to one product: the staff
    account, if its console products include `product`, otherwise 403."""

    def check(account: Account = Depends(current_console_account)) -> Account:
        if product not in (account.console_products or []):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "This account cannot open that product")
        return account

    return check


def console_role(product: str, roles: tuple[str, ...]):
    """Like console_product, but also requires one of `roles`. For console actions
    that change something, which readonly, support and reviewer staff may not do."""

    def check(account: Account = Depends(console_product(product))) -> Account:
        if account.console_role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Your role cannot do this")
        return account

    return check


def account_from_api_key(
    request: Request,
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
    if key.revoked_at is not None or key.suspended_at is not None or _expired(key.expires_at, now):
        raise refused

    account = db.get(Account, key.account_id)
    if account is None or not account.is_active:
        raise refused

    # Written on every call, so a key nobody uses can be found and removed. It is
    # a timestamp on a row the caller already proved they hold: no extra risk.
    key.last_used_at = now
    db.commit()
    set_actor(request, account.id, "api_key", api_key=key.prefix)
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


@dataclass(frozen=True)
class Caller:
    """Who is asking, and how much they may see."""

    account: Account
    staff: bool

    @property
    def scope(self) -> int | None:
        """The one account whose data this caller may see, or None for
        staff, who see every account's."""
        return None if self.staff else self.account.id

    def scope_for(self, client_id: int | None) -> int | None:
        """The account to filter by: a client always gets its own, whatever it asks
        for; staff get the client they asked for, or every client."""
        return client_id if self.staff else self.account.id


def current_caller(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    x_api_key: str | None = Header(default=None, alias=_API_KEY_HEADER),
    db: Session = Depends(get_session),
) -> Caller:
    """A client signed in to the dashboard, a client's API key, or staff
    signed in to the console. A bearer token is tried as a console token first:
    the two kinds of token are signed apart, so neither passes for the other."""
    if credentials is not None:
        if security.read_console_token(credentials.credentials) is not None:
            staff = current_console_account(request, credentials, db)
            if "kyc" not in (staff.console_products or []):
                raise HTTPException(status.HTTP_403_FORBIDDEN, "This account cannot open that product")
            return Caller(account=staff, staff=True)
        return Caller(account=current_account(request, credentials, db), staff=False)
    if x_api_key:
        return Caller(account=account_from_api_key(request, x_api_key, db), staff=False)
    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "Sign in first",
        headers={"WWW-Authenticate": "Bearer"},
    )


def current_client(caller: Caller = Depends(current_caller)) -> Account:
    """Like current_caller, for what only a client account does for itself, such
    as making its own API keys. Staff have the console for theirs."""
    if caller.staff:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This is for client accounts; staff use the console")
    return caller.account


def legacy_only() -> None:
    """Hide a first-generation endpoint unless ENABLE_LEGACY_ENDPOINTS is on.

    These endpoints take no API key and no session token. Answering 404 rather than
    401 says nothing about what exists at the address.
    """
    if not settings.ENABLE_LEGACY_ENDPOINTS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
