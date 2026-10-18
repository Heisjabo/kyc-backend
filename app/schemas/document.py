from datetime import date
from enum import Enum

from pydantic import BaseModel


class ChosenDocumentGroup(BaseModel):
    """The section of the document step the person ticked, when the client set
    document_groups: its key, and the label they read when they ticked it."""

    key: str
    label: str | None = None


class DocumentType(str, Enum):
    passport = "passport"
    id_card = "id_card"
    driving_license = "driving_license"
    # A child's document. It carries no photograph, no expiry and no MRZ, so the
    # checks that depend on those are skipped rather than failed.
    birth_certificate = "birth_certificate"


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


class DocumentFrameProbe(BaseModel):
    """Live guidance for one camera frame while a document is being held up."""

    # True when nothing needs fixing, so the page may take the picture.
    usable: bool
    # How much of the rectangle the card covers, 0 to 1.
    card_fill: float | None = None
    # Lines of printed text seen: what says a document is in view at all.
    text_lines: int | None = None
    # The portrait's height as a share of the card's, or None if no face is seen.
    portrait_ratio: float | None = None
    blur_score: float | None = None
    brightness: float | None = None
    glare_ratio: float | None = None
    # FIT_THE_CARD_IN_THE_BOX, TURN_THE_CARD_UPRIGHT, SHOW_THE_BACK, PORTRAIT_NOT_VISIBLE,
    # MOVE_CLOSER, TOO_DARK, TOO_BRIGHT, GLARE_ON_THE_CARD, HOLD_STILL
    hints: list[str] = []
