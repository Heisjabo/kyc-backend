"""What a client sends to open a verification, and what they get back.

Field names follow Shufti's where they mean the same thing (reference,
callback_url, redirect_url, verification_mode, country, language, ttl), so a
client already integrated with them can point at us with almost no change.
"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from app.core import urls
from app.core.config import settings
from app.schemas.console import ConsoleApplicant, ConsoleCheck, ConsoleClient, ConsoleDocument, ConsoleFile, ConsoleReviewEvent
from app.schemas.document import ChosenDocumentGroup
from app.screening.yente import ScreeningResult


class ClaimedDetails(BaseModel):
    """What the client says about the person, before anything is proved."""

    first_name: str | None = Field(default=None, max_length=80)
    last_name: str | None = Field(default=None, max_length=80)
    date_of_birth: date | None = None
    country: str | None = Field(default=None, max_length=80)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=32)
    address: str | None = Field(default=None, max_length=250)


IdentityDocument = Literal["passport", "id_card", "driving_license"]


class DocumentGroup(BaseModel):
    """One section on the document step: who it is for, and what it accepts.

    The person ticks the section that describes them and is then offered only
    that section's documents.
    """

    key: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9_]+$")
    label: str = Field(min_length=1, max_length=160)
    # What the section asks for, shown under the label: "Passport required".
    note: str | None = Field(default=None, max_length=160)
    document_types: list[IdentityDocument] = Field(min_length=1)
    # What to call a document in this section, where the plain name is wrong:
    # an id_card is a "Resident ID" to somebody who lives in a country they are
    # not a citizen of.
    labels: dict[IdentityDocument, str] = Field(default_factory=dict)

    @field_validator("document_types")
    @classmethod
    def _once_each(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))

    @field_validator("labels")
    @classmethod
    def _short_labels(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not label.strip() or len(label) > 60 for label in value.values()):
            raise ValueError("each label needs 1 to 60 characters")
        return value


# The sections a client gets by sending document_groups: "residency". Citizens
# show a passport, or a national ID or licence when they hold no passport;
# foreigners show the resident ID their country of residence gave them.
DOCUMENT_GROUP_PRESETS: dict[str, list[DocumentGroup]] = {
    "residency": [
        DocumentGroup(
            key="citizen_passport",
            label="I am a citizen of my registered country",
            note="Passport required",
            document_types=["passport"],
        ),
        DocumentGroup(
            key="citizen_no_passport",
            label="I don't have a passport but I am a citizen of my registered country",
            note="Driver's license or national ID required",
            document_types=["driving_license", "id_card"],
            labels={"id_card": "National ID"},
        ),
        DocumentGroup(
            key="foreign_resident",
            label="I am a foreigner living or working in my registered country",
            note="Resident ID required",
            document_types=["id_card"],
            labels={"id_card": "Resident ID"},
        ),
    ],
}


class CreateVerificationRequest(BaseModel):
    # The client's own string for this verification, which they look statuses up
    # by afterwards. Must be unique within their account. One is generated for
    # them if they send none.
    reference: str | None = Field(default=None, min_length=6, max_length=250)
    services: list[str] = Field(
        default_factory=lambda: ["document", "face", "aml"],
        description="document, face, address, due_diligence, aml, consent",
    )
    applicant: ClaimedDetails = Field(default_factory=ClaimedDetails)

    callback_url: str | None = None
    redirect_url: str | None = None
    country: str | None = Field(default=None, max_length=3)
    language: str | None = Field(default="EN", max_length=8)
    verification_mode: str | None = Field(default="any", max_length=16)
    form_id: str | None = Field(default=None, max_length=120)
    # The identity documents the person may choose from, as Shufti's
    # document.supported_types. Left out, every adult document is offered.
    document_types: list[IdentityDocument] | None = Field(
        default=None, description="passport, id_card, driving_license"
    )
    # Sections the person chooses between before choosing a document: the name
    # of a preset ("residency"), or the client's own sections. Left out, the
    # document step is the plain list of document_types.
    document_groups: Literal["residency"] | list[DocumentGroup] | None = Field(
        default=None, description='"residency", or a list of {key, label, note, document_types, labels}'
    )
    # The proofs of address the person may choose from, as Shufti's
    # address.supported_types. Left out, every proof except a driving licence.
    address_types: list[
        Literal["utility_bill", "bank_statement", "credit_card_statement", "rent_agreement", "driving_license"]
    ] | None = Field(
        default=None,
        description="utility_bill, bank_statement, credit_card_statement, rent_agreement, driving_license",
    )
    # Minutes, as Shufti defines it: the link stops working this long after it
    # is created. Their default is 60, and so is ours. Longer is allowed up to
    # MAX_SESSION_TTL_MINUTES, so a client can send somebody a link that lasts a
    # week (10080) or a month (43200) when the person is expected to take their
    # time. ttl_days is the same setting for people who would rather say "7".
    ttl: int | None = Field(default=None, ge=1, description="Minutes the session stays open")
    ttl_days: int | None = Field(default=None, ge=1, le=30, description="Days the session stays open")

    @field_validator("callback_url", "redirect_url")
    @classmethod
    def _safe_url(cls, value, info):
        """Where we send data (callback) and where we send the person (redirect)."""
        if value is None or value == "":
            return None
        check = urls.check_callback if info.field_name == "callback_url" else urls.check_redirect
        try:
            return check(value)
        except urls.UnsafeUrl as problem:
            raise ValueError(f"{info.field_name} {problem}") from None

    @field_validator("document_types", "address_types")
    @classmethod
    def _document_types(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("needs at least one type, or leave it out")
        # Once each, in the order sent.
        return list(dict.fromkeys(value))

    @field_validator("document_groups")
    @classmethod
    def _document_groups(cls, value):
        if value is None:
            return None
        if isinstance(value, str):
            return [group.model_copy(deep=True) for group in DOCUMENT_GROUP_PRESETS[value]]
        if not 1 <= len(value) <= 6:
            raise ValueError("needs 1 to 6 groups, or leave it out")
        keys = [group.key for group in value]
        if len(set(keys)) != len(keys):
            raise ValueError("each group needs its own key")
        return value

    @model_validator(mode="after")
    def _groups_within_types(self):
        """The groups offer documents; document_types, if sent, is the ceiling."""
        if not self.document_groups:
            return self
        offered = list(dict.fromkeys(kind for group in self.document_groups for kind in group.document_types))
        if self.document_types is None:
            self.document_types = offered
        elif outside := [kind for kind in offered if kind not in self.document_types]:
            raise ValueError(f"document_groups offer {', '.join(outside)}, which document_types leaves out")
        return self

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
    # The document section the person ticked; None without document_groups.
    document_group: ChosenDocumentGroup | None = None
    aml: ScreeningResult | None = None
    expires_at: datetime | None
    created_at: datetime
    completed_at: datetime | None


class DetailStep(StepResponse):
    """A step with the checks it ran, for whoever reads a verification in full."""

    checks: list[ConsoleCheck] = []
    match_score: float | None = None
    match_threshold: float | None = None


class VerificationDetailResponse(VerificationResponse):
    """One verification in full, for GET /v1/verifications/{reference}: the
    response clients have always had, with what was read from the document,
    every file captured, and who it is for alongside. Every field of
    VerificationResponse is still here and means the same."""

    id: str
    document_type: str | None
    client: ConsoleClient
    applicant: ConsoleApplicant
    started_at: datetime | None
    steps: list[DetailStep]
    document: ConsoleDocument | None
    files: list[str]
    evidence: list[ConsoleFile]
    # Decisions the compliance team made on it, oldest first.
    reviews: list[ConsoleReviewEvent] = []


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
    expires_at: datetime | None = None
    redirect_url: str | None
    # The identity documents offered on the document step; None offers all of them.
    document_types: list[str] | None = None
    # The sections offered before the documents; None shows document_types as one list.
    document_groups: list[DocumentGroup] | None = None
    # The proofs offered on the address step; None offers all but a driving licence.
    address_types: list[str] | None = None


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
