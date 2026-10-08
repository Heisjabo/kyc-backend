"""Signing in to the admin console (pangolin-admin).

Staff are accounts with a console role. They sign in here with their email and
password and get a console token, which only the console endpoints accept.

The refusals are the same as at the client sign-in: a wrong email, a wrong
password and an account without a console role all get one answer, so this
endpoint cannot be used to learn which addresses are staff.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_console_account
from app.core import security
from app.db.models import Account, AuditEvent
from app.db.session import get_session
from app.schemas.auth import LoginRequest
from app.schemas.console import ConsolePasswordChange, ConsoleProfileUpdate, ConsoleTokenResponse, ConsoleUserResponse
from app.services.network import client_ip

router = APIRouter(prefix="/console/auth", tags=["admin console"])

_WRONG = "Email or password is wrong"


def _as_user(account: Account) -> ConsoleUserResponse:
    return ConsoleUserResponse(
        id=str(account.id),
        email=account.email,
        first_name=account.first_name,
        last_name=account.last_name,
        role=account.console_role or "",
        products=list(account.console_products or []),
        last_login_at=account.last_login_at,
    )


def _record(db: Session, request: Request, account: Account, event_type: str) -> None:
    """A sign-in by a staff account, kept in the audit log. Refusals for an email
    that is not staff are not recorded: there is nobody to attribute them to."""
    db.add(
        AuditEvent(
            event_type=event_type,
            actor=str(account.id),
            payload={"by": account.email, "ip": client_ip(request), "user_agent": request.headers.get("user-agent")},
        )
    )
    db.commit()


@router.post("/login", response_model=ConsoleTokenResponse)
def login(body: LoginRequest, request: Request, db: Session = Depends(get_session)) -> ConsoleTokenResponse:
    account = db.scalar(select(Account).where(Account.email == body.email.lower().strip()))
    # Verified even when there is no such account, so every refusal takes as long.
    stored = account.password_hash if account else security.hash_password("no such account")
    correct = security.verify_password(body.password, stored)

    if account is None or not account.console_role:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, _WRONG)
    if not correct:
        _record(db, request, account, "console.sign_in_failed")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, _WRONG)
    if not account.is_active:
        _record(db, request, account, "console.sign_in_refused")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account has been suspended")

    if security.needs_rehash(account.password_hash):
        account.password_hash = security.hash_password(body.password)
    account.last_login_at = datetime.now(UTC)
    _record(db, request, account, "console.sign_in")
    db.refresh(account)

    token, seconds = security.issue_console_token(
        str(account.id), account.email, account.console_role, list(account.console_products or [])
    )
    return ConsoleTokenResponse(access_token=token, expires_in=seconds, user=_as_user(account))


@router.get("/me", response_model=ConsoleUserResponse)
def me(account: Account = Depends(current_console_account)) -> ConsoleUserResponse:
    return _as_user(account)


@router.patch("/me", response_model=ConsoleUserResponse)
def update_me(
    body: ConsoleProfileUpdate,
    request: Request,
    account: Account = Depends(current_console_account),
    db: Session = Depends(get_session),
) -> ConsoleUserResponse:
    """Change the signed-in member's own name."""
    before = {"first_name": account.first_name, "last_name": account.last_name}
    account.first_name = body.first_name.strip()
    account.last_name = body.last_name.strip()
    db.add(
        AuditEvent(
            event_type="console.profile_updated",
            actor=str(account.id),
            payload={"by": account.email, "before": before, "after": {"first_name": account.first_name, "last_name": account.last_name}, "ip": client_ip(request)},
        )
    )
    db.commit()
    db.refresh(account)
    return _as_user(account)


@router.post("/password", response_model=ConsoleUserResponse)
def change_password(
    body: ConsolePasswordChange,
    request: Request,
    account: Account = Depends(current_console_account),
    db: Session = Depends(get_session),
) -> ConsoleUserResponse:
    """Change the signed-in member's own password. The current one is required."""
    if not security.verify_password(body.current_password, account.password_hash):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "The current password is wrong")
    if body.new_password == body.current_password:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "The new password is the same as the current one")
    account.password_hash = security.hash_password(body.new_password)
    db.add(
        AuditEvent(
            event_type="console.password_changed",
            actor=str(account.id),
            payload={"by": account.email, "ip": client_ip(request)},
        )
    )
    db.commit()
    db.refresh(account)
    return _as_user(account)
