"""What the auth endpoints take and return.

A password is never echoed, a hash never leaves the service, and an API key is
returned exactly once: in the response that created it.
"""

from datetime import date, datetime

from pydantic import BaseModel, EmailStr, Field

from app.core.config import settings

PASSWORD = Field(min_length=settings.MIN_PASSWORD_LENGTH, max_length=settings.MAX_PASSWORD_LENGTH)


class RegisterRequest(BaseModel):
    """What someone gives to open an account.

    The same details tuesday-backend asks for at signup, without its trading
    fields (role, amount, stock), its entity representatives, or ethnicity.
    """

    email: EmailStr
    password: str = PASSWORD
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
    phone: str = Field(min_length=5, max_length=32)
    country_code: str = Field(min_length=1, max_length=8, description="Dialling code, e.g. +250")
    date_of_birth: date
    country: str = Field(min_length=2, max_length=80)

    # Asked for, kept when given, not required.
    gender: str | None = Field(default=None, max_length=32)
    city: str | None = Field(default=None, max_length=80)
    address: str | None = Field(default=None, max_length=200)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class AccountResponse(BaseModel):
    id: str
    email: str
    first_name: str
    last_name: str
    phone: str
    country_code: str
    date_of_birth: date
    country: str
    gender: str | None
    city: str | None
    address: str | None
    is_active: bool
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    account: AccountResponse


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ForgotPasswordResponse(BaseModel):
    """Always the same answer, whether or not the address is one of ours: telling
    a stranger which email addresses have accounts is a list worth stealing."""

    message: str
    # Only on a developer's machine, and only while there is no email service.
    reset_token: str | None = None


class ResetPasswordRequest(BaseModel):
    token: str
    password: str = PASSWORD


class CreateApiKeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80, description="What this key is for")
    expires_in_days: int = Field(
        default=90,
        ge=1,
        le=settings.MAX_API_KEY_DAYS,
        description="Keys expire. One that lives forever is one nobody rotates.",
    )


class ApiKeyResponse(BaseModel):
    id: str
    name: str
    prefix: str
    expires_at: datetime
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    # Set while Pangolin has suspended the key; it does not work until resumed.
    suspended_at: datetime | None = None
    is_valid: bool


class CreatedApiKeyResponse(ApiKeyResponse):
    """The one and only time the key itself is returned."""

    key: str
    warning: str = "Copy this key now. It is not stored and cannot be shown again."
