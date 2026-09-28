from datetime import date
from enum import Enum

from pydantic import BaseModel

from app.schemas.document import CheckResult, Decision


class ProofType(str, Enum):
    """What the person is uploading, matching the supported_types we send Shufti."""

    utility_bill = "utility_bill"
    bank_statement = "bank_statement"
    credit_card_statement = "credit_card_statement"
    rent_agreement = "rent_agreement"


class AddressExtracted(BaseModel):
    """What the check read, in the two halves Shufti's report prints.

    `full_address` is what the client submitted at initiation; everything else was
    read off the document.
    """

    document_type: ProofType
    full_address: str
    # "street", "city" or "country": how closely the two sides agreed.
    matched_level: str | None = None
    matched_places: list[str] = []
    proof_date: date | None = None
    proof_age_days: int | None = None


class AddressCheckResponse(BaseModel):
    decision: Decision
    reasons: list[str]
    warnings: list[str]
    extracted: AddressExtracted
    checks: list[CheckResult]
    processing_ms: int
