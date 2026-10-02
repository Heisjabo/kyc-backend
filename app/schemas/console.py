"""What the admin console's endpoints take and return.

The console is pangolin-admin, used by Pangolin staff for both products. Its
sign-in reuses the account's email and password; only the response differs, in
saying what the person may do there. Its verification views see every client's
verifications, unlike the client API, which is scoped to one account.
"""

from datetime import date, datetime

from pydantic import BaseModel


class ConsoleUserResponse(BaseModel):
    id: str
    email: str
    first_name: str
    last_name: str
    role: str
    products: list[str]
    last_login_at: datetime | None


class ConsoleTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: ConsoleUserResponse


# Verifications, as staff see them: every client's, not just one.

class ConsoleClient(BaseModel):
    """The client account a verification belongs to."""

    id: str
    name: str
    email: str


class ConsoleApplicant(BaseModel):
    """The person being verified, from what they claimed or what was read."""

    customer_id: str | None
    name: str | None
    email: str | None
    country: str | None


class ConsoleVerificationSummary(BaseModel):
    id: str
    reference: str
    status: str
    event: str | None
    services: list[str]
    document_type: str | None
    reasons: list[str]
    client: ConsoleClient
    applicant: ConsoleApplicant
    created_at: datetime
    completed_at: datetime | None
    expires_at: datetime | None


class ConsoleVerificationList(BaseModel):
    items: list[ConsoleVerificationSummary]
    total: int
    page: int
    page_size: int
    # How many match the other filters, for each status, so the console can put
    # a count on every status filter at once.
    status_counts: dict[str, int]


class ConsoleCheck(BaseModel):
    name: str
    status: str
    reasons: list[str]
    warnings: list[str]


class ConsoleStep(BaseModel):
    step: str
    attempt: int
    status: str
    reasons: list[str]
    warnings: list[str]
    checks: list[ConsoleCheck]
    match_score: float | None
    match_threshold: float | None
    started_at: datetime | None
    completed_at: datetime | None


class ConsoleDocument(BaseModel):
    """What was read from the identity document. The number is shortened to its
    last four characters: enough to compare, not enough to reuse."""

    document_type: str
    decision: str
    full_name: str | None
    date_of_birth: date | None
    document_number_last4: str | None
    issuing_country: str | None
    nationality: str | None
    expiry_date: date | None


class ConsoleFile(BaseModel):
    """One piece of evidence captured during a verification. Fetch its bytes from
    /v1/console/verifications/{id}/files/{file id}, and a still from a video
    from the same path with /thumbnail."""

    id: str
    kind: str
    step: str | None
    attempt: int | None
    content_type: str
    byte_size: int
    captured_at: datetime | None
    created_at: datetime


class ConsoleVerificationDetail(ConsoleVerificationSummary):
    language: str | None
    warnings: list[str]
    started_at: datetime | None
    steps: list[ConsoleStep]
    document: ConsoleDocument | None
    files: list[str]
    # Every file captured, oldest first, retakes included.
    evidence: list[ConsoleFile]
