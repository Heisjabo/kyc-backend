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
    aml: dict | None = None
    language: str | None
    warnings: list[str]
    started_at: datetime | None
    steps: list[ConsoleStep]
    document: ConsoleDocument | None
    files: list[str]
    # Every file captured, oldest first, retakes included.
    evidence: list[ConsoleFile]


# Client accounts, as staff see them: every organisation calling the API.

class ConsoleClientOption(BaseModel):
    """Enough to name a client in a filter."""

    id: str
    name: str
    email: str


class ConsoleClientSummary(BaseModel):
    id: str
    name: str
    email: str
    phone: str
    country: str
    status: str  # "active" or "suspended"
    created_at: datetime
    last_login_at: datetime | None
    # Counted with the status a client would see now, so a lapsed session is expired.
    verifications: int
    passed: int
    failed: int
    needs_review: int
    last_verification_at: datetime | None
    # Neither revoked nor past their expiry.
    active_keys: int


class ConsoleClientOverview(BaseModel):
    """Totals across every client, whatever the filters, for the page's headline numbers."""

    clients: int
    active: int
    suspended: int
    active_keys: int
    verifications: int


class ConsoleClientList(BaseModel):
    items: list[ConsoleClientSummary]
    total: int
    page: int
    page_size: int
    overview: ConsoleClientOverview
    # The countries clients have signed up from, for the country filter.
    countries: list[str]


class ConsoleApiKey(BaseModel):
    """A key as its owner sees it in their list: never the key itself."""

    name: str
    prefix: str
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class ConsoleClientDetail(ConsoleClientSummary):
    first_name: str
    last_name: str
    country_code: str
    city: str | None
    address: str | None
    in_progress: int
    applicants: int
    api_keys: list[ConsoleApiKey]
    # The five most recent, newest first.
    recent_verifications: list[ConsoleVerificationSummary]


# API keys across every client.

class ConsoleApiKeyRow(ConsoleApiKey):
    id: str
    # "active", "expiring" (within 30 days), "expired" or "revoked".
    state: str
    client: ConsoleClient


class ConsoleApiKeyList(BaseModel):
    items: list[ConsoleApiKeyRow]
    total: int
    page: int
    page_size: int
    # How many match the other filters, for each state.
    state_counts: dict[str, int]


# Applicants: the people being verified, across every client.

class ConsoleApplicantSummary(BaseModel):
    id: str
    customer_id: str
    client_customer_id: str | None
    name: str | None
    email: str | None
    phone: str | None
    country: str | None
    client: ConsoleClient
    created_at: datetime
    verifications: int
    # The applicant's most recent verification, by the status a client would see now.
    latest_status: str | None
    latest_at: datetime | None


class ConsoleApplicantList(BaseModel):
    items: list[ConsoleApplicantSummary]
    total: int
    page: int
    page_size: int
    # By latest status, under the other filters; "none" counts applicants with no verification.
    status_counts: dict[str, int]
    countries: list[str]


class ConsoleApplicantDetail(ConsoleApplicantSummary):
    first_name: str | None
    last_name: str | None
    date_of_birth: date | None
    # Every verification, newest first.
    history: list[ConsoleVerificationSummary]
