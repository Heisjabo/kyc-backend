"""What a client sends to open a verification, and what they get back.

Field names follow Shufti's where they mean the same thing (reference,
callback_url, redirect_url, verification_mode, country, language, ttl), so a
client already integrated with them can point at us with almost no change.
"""

from datetime import date, datetime

from pydantic import BaseModel, EmailStr, Field, model_validator

from app.core.config import settings


class ClaimedDetails(BaseModel):
    """What the client says about the person, before anything is proved."""

    first_name: str | None = Field(default=None, max_length=80)
    last_name: str | None = Field(default=None, max_length=80)
    date_of_birth: date | None = None
    country: str | None = Field(default=None, max_length=80)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=32)
    address: str | None = Field(default=None, max_length=250)


class CreateVerificationRequest(BaseModel):
    # The client's own string for this verification, which they look statuses up
    # by afterwards. Must be unique within their account. One is generated for
    # them if they send none.
    reference: str | None = Field(default=None, min_length=6, max_length=250)
    services: list[str] = Field(
        default_factory=lambda: ["document", "face"],
        description="document, face, address, due_diligence, aml, consent",
    )
    applicant: ClaimedDetails = Field(default_factory=ClaimedDetails)

    callback_url: str | None = None
    redirect_url: str | None = None
    country: str | None = Field(default=None, max_length=3)
    language: str | None = Field(default="EN", max_length=8)
    verification_mode: str | None = Field(default="any", max_length=16)
    form_id: str | None = Field(default=None, max_length=120)
    # Minutes, as Shufti defines it: the link stops working this long after it
    # is created. Their default is 60, and so is ours. Longer is allowed up to
    # MAX_SESSION_TTL_MINUTES, so a client can send somebody a link that lasts a
    # week (10080) or a month (43200) when the person is expected to take their
    # time. ttl_days is the same setting for people who would rather say "7".
    ttl: int | None = Field(default=None, ge=1, description="Minutes the session stays open")
    ttl_days: int | None = Field(default=None, ge=1, le=30, description="Days the session stays open")

    @model_validator(mode="after")
    def _one_ttl(self):
        if self.ttl and self.ttl_days:
            raise ValueError("Send ttl or ttl_days, not both")
        if self.ttl_days:
            self.ttl = self.ttl_days * 24 * 60
        if self.ttl and self.ttl > settings.MAX_SESSION_TTL_MINUTES:
            raise ValueError(
                f"ttl may not exceed {settings.MAX_SESSION_TTL_MINUTES} minutes "
                f"({settings.MAX_SESSION_TTL_MINUTES // (24 * 60)} days)"
            )
        return self


class StepResponse(BaseModel):
    step: str
    attempt: int
    status: str
    reasons: list[str]
    warnings: list[str]
    extracted: dict
    started_at: datetime | None
    completed_at: datetime | None


class VerificationResponse(BaseModel):
    reference: str
    customer_id: str | None
    status: str
    event: str | None
    services: list[str]
    session_url: str
    country: str | None
    language: str | None
    reasons: list[str]
    warnings: list[str]
    steps: list[StepResponse]
    expires_at: datetime | None
    created_at: datetime
    completed_at: datetime | None


class SessionResponse(BaseModel):
    """What the page the person is looking at needs to know.

    Deliberately thin: it is served on a token anybody with the link holds, so
    it carries no reasons, no extracted fields and nothing about the client.
    """

    reference: str
    status: str
    services: list[str]
    next_step: str | None
    completed_steps: list[str]
    expired: bool
    redirect_url: str | None


class VerifyChildRequest(BaseModel):
    """A child added to a parent who has already been verified.

    The parent is named by their verification, not by anything typed here: their
    names come off their own verified record, so a caller cannot claim a
    certificate belongs to a name they invented.
    """

    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
    date_of_birth: date
    relationship: str | None = Field(default=None, max_length=32, description="parent, guardian")


class ChildVerificationResponse(BaseModel):
    reference: str
    parent_reference: str
    customer_id: str | None
    status: str
    decision: str
    reasons: list[str]
    warnings: list[str]
    checks: list[dict]
    extracted: dict
    created_at: datetime
