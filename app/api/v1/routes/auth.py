"""Accounts: registering, signing in, and forgetting a password.

An account is a customer of this service, the organisation calling the API. It
is not a person being verified; those are `users`, and they never sign in.

Two deliberate refusals to be helpful:

  * A failed sign-in says only that the email or password was wrong, never which.
  * Asking to reset a password always gives the same answer, whether or not the
    address belongs to an account. Confirming which addresses exist hands over a
    list worth stealing, and the person who owns the address learns nothing from
    the difference anyway.
"""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_account
from app.core import security
from app.core.config import settings
from app.db.models import Account, PasswordReset
from app.db.session import get_session
from app.schemas.auth import (
    AccountResponse,
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    LoginRequest,
    RegisterRequest,
    ResetPasswordRequest,
    TokenResponse,
)
from app.services import email as email_service

router = APIRouter(prefix="/auth", tags=["accounts"])

_SAME_ANSWER = "If that address has an account, a link to set a new password is on its way."


def _as_response(account: Account) -> AccountResponse:
    return AccountResponse(
        id=str(account.id),
        email=account.email,
        first_name=account.first_name,
        last_name=account.last_name,
        phone=account.phone,
        country_code=account.country_code,
        date_of_birth=account.date_of_birth,
        country=account.country,
        gender=account.gender,
        city=account.city,
        address=account.address,
        is_active=account.is_active,
        created_at=account.created_at,
    )


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register(body: RegisterRequest, db: Session = Depends(get_session)) -> TokenResponse:
    """Open an account and sign in straight away."""
    email = body.email.lower().strip()
    if db.scalar(select(Account).where(Account.email == email)):
        # Registration cannot hide which addresses are taken: the address either
        # becomes this person's account or it does not.
        raise HTTPException(status.HTTP_409_CONFLICT, "That email address already has an account")

    def given(value: str | None) -> str | None:
        return (value or "").strip() or None

    account = Account(
        email=email,
        password_hash=security.hash_password(body.password),
        first_name=body.first_name.strip(),
        last_name=body.last_name.strip(),
        phone=body.phone.strip(),
        country_code=body.country_code.strip(),
        date_of_birth=body.date_of_birth,
        country=body.country.strip(),
        gender=given(body.gender),
        city=given(body.city),
        address=given(body.address),
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return _token_for(account, db)


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, db: Session = Depends(get_session)) -> TokenResponse:
    account = db.scalar(select(Account).where(Account.email == body.email.lower().strip()))
    # The password is verified even when there is no such account, so that a
    # wrong address and a wrong password take the same time to answer.
    stored = account.password_hash if account else security.hash_password("no such account")
    correct = security.verify_password(body.password, stored)

    if account is None or not correct:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Email or password is wrong")
    if not account.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account has been suspended")

    # Upgrade the stored hash quietly if the parameters have moved on since.
    if security.needs_rehash(account.password_hash):
        account.password_hash = security.hash_password(body.password)
    return _token_for(account, db)


@router.get("/me", response_model=AccountResponse)
def me(account: Account = Depends(current_account)) -> AccountResponse:
    return _as_response(account)


@router.post("/forgot-password", response_model=ForgotPasswordResponse)
def forgot_password(body: ForgotPasswordRequest, db: Session = Depends(get_session)) -> ForgotPasswordResponse:
    """Start a password reset. The answer never says whether the address is known."""
    account = db.scalar(select(Account).where(Account.email == body.email.lower().strip()))
    revealed = None

    if account is not None and account.is_active:
        token, token_hash = security.new_reset_token()
        db.add(
            PasswordReset(
                account_id=account.id,
                token_hash=token_hash,
                expires_at=datetime.now(UTC) + timedelta(minutes=settings.PASSWORD_RESET_MINUTES),
            )
        )
        db.commit()
        email_service.send_password_reset(account.email, token)
        if email_service.reveal_token_to_caller():
            revealed = token

    return ForgotPasswordResponse(message=_SAME_ANSWER, reset_token=revealed)


@router.post("/reset-password", response_model=AccountResponse)
def reset_password(body: ResetPasswordRequest, db: Session = Depends(get_session)) -> AccountResponse:
    """Set a new password with a token from the reset email.

    The token works once. The row it came from is kept, marked as used, so a
    reset can be accounted for afterwards.
    """
    reset = db.scalar(
        select(PasswordReset).where(PasswordReset.token_hash == security.fingerprint(body.token))
    )
    now = datetime.now(UTC)
    if reset is None or reset.used_at is not None or _expired(reset.expires_at, now):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That link has expired or has already been used")

    account = db.get(Account, reset.account_id)
    if account is None or not account.is_active:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That link has expired or has already been used")

    account.password_hash = security.hash_password(body.password)
    reset.used_at = now
    # Any other reset still outstanding is spent too: asking twice and using the
    # older link should not work after the newer one has been used.
    for other in db.scalars(
        select(PasswordReset).where(
            PasswordReset.account_id == account.id, PasswordReset.used_at.is_(None)
        )
    ):
        other.used_at = now
    db.commit()
    db.refresh(account)
    return _as_response(account)


def _expired(when: datetime, now: datetime) -> bool:
    """Compare safely whatever the database hands back: SQLite returns naive
    datetimes where Postgres returns aware ones."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when < now


def _token_for(account: Account, db: Session) -> TokenResponse:
    account.last_login_at = datetime.now(UTC)
    db.commit()
    db.refresh(account)
    token, seconds = security.issue_access_token(str(account.id), account.email)
    return TokenResponse(access_token=token, expires_in=seconds, account=_as_response(account))
