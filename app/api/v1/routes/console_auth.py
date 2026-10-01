"""Signing in to the admin console (pangolin-admin).

Staff are accounts with a console role. They sign in here with their email and
password and get a console token, which only the console endpoints accept.

The refusals are the same as at the client sign-in: a wrong email, a wrong
password and an account without a console role all get one answer, so this
endpoint cannot be used to learn which addresses are staff.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_console_account
from app.core import security
from app.db.models import Account
from app.db.session import get_session
from app.schemas.auth import LoginRequest
from app.schemas.console import ConsoleTokenResponse, ConsoleUserResponse

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


@router.post("/login", response_model=ConsoleTokenResponse)
def login(body: LoginRequest, db: Session = Depends(get_session)) -> ConsoleTokenResponse:
    account = db.scalar(select(Account).where(Account.email == body.email.lower().strip()))
    # Verified even when there is no such account, so every refusal takes as long.
    stored = account.password_hash if account else security.hash_password("no such account")
    correct = security.verify_password(body.password, stored)

    if account is None or not correct or not account.console_role:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, _WRONG)
    if not account.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account has been suspended")

    if security.needs_rehash(account.password_hash):
        account.password_hash = security.hash_password(body.password)
    account.last_login_at = datetime.now(UTC)
    db.commit()
    db.refresh(account)

    token, seconds = security.issue_console_token(
        str(account.id), account.email, account.console_role, list(account.console_products or [])
    )
    return ConsoleTokenResponse(access_token=token, expires_in=seconds, user=_as_user(account))


@router.get("/me", response_model=ConsoleUserResponse)
def me(account: Account = Depends(current_console_account)) -> ConsoleUserResponse:
    return _as_user(account)
