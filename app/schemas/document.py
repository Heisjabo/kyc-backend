from datetime import date
from enum import Enum

from pydantic import BaseModel


class DocumentType(str, Enum):
    passport = "passport"
    id_card = "id_card"
    driving_license = "driving_license"


class Decision(str, Enum):
    passed = "pass"
    failed = "fail"
    review = "review"
    retake = "retake"


class CheckStatus(str, Enum):
    passed = "pass"
    failed = "fail"
    review = "review"
    skipped = "skipped"


class Applicant(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    date_of_birth: date | None = None
    issuing_country: str | None = None


class CheckResult(BaseModel):
    name: str
    status: CheckStatus
    reasons: list[str] = []
    # Worth knowing, but not enough on their own to change the decision.
    warnings: list[str] = []
    details: dict = {}


class ExtractedFields(BaseModel):
    # "mrz" when read from the machine readable zone, "visual" when read from the printed text.
    source: str
    document_type_code: str | None = None
    document_number: str | None = None
    issuing_country: str | None = None
    nationality: str | None = None
    surname: str | None = None
    given_names: str | None = None
    full_name: str | None = None
    date_of_birth: date | None = None
    issue_date: date | None = None
    expiry_date: date | None = None
    sex: str | None = None


class DocumentCheckResponse(BaseModel):
    # Opened when the document carries a usable portrait, so the face step can
    # reuse it. Held in memory only, for SESSION_TTL_MINUTES.
    verification_id: str | None = None
    face_verification_url: str | None = None
    # "ok", "small" or "missing": the state of the photo printed on the document.
    portrait: str | None = None
    decision: Decision
    reasons: list[str]
    warnings: list[str]
    document_type: DocumentType
    extracted: ExtractedFields | None
    checks: list[CheckResult]
    processing_ms: int
