"""What the admin console's endpoints take and return.

The console is pangolin-admin, used by Pangolin staff for both products. Its
sign-in reuses the account's email and password; only the response differs, in
saying what the person may do there. Its verification views see every client's
verifications, unlike the client API, which is scoped to one account.
"""

from datetime import date, datetime

from pydantic import BaseModel, Field


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
    # Decisions staff have made on it, oldest first.
    reviews: list["ConsoleReviewEvent"] = []
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
    suspended_at: datetime | None


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

class ConsoleApiKeyAction(BaseModel):
    """The latest thing console staff did to a key."""

    action: str  # "suspended", "resumed" or "expired"
    by: str  # the staff member's email
    reason: str | None
    at: datetime


class ConsoleApiKeyActionRequest(BaseModel):
    """Why a key is being suspended, resumed or expired. Optional, kept in the audit log."""

    reason: str | None = Field(default=None, max_length=500)


class ConsoleApiKeyRow(ConsoleApiKey):
    id: str
    # "active", "expiring" (within 30 days), "suspended", "expired" or "revoked".
    state: str
    client: ConsoleClient
    last_action: ConsoleApiKeyAction | None = None


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


# AML screening, as staff see it: searches run by hand from the console, and
# the screenings the AML step ran on real verifications.

class ConsoleAmlSearchRequest(BaseModel):
    """A person to look up on the watchlists. Only the name is required; a date
    of birth, nationality or ID number sharpen the match."""

    full_name: str = Field(min_length=2, max_length=250)
    date_of_birth: date | None = None
    nationality: str | None = Field(default=None, min_length=2, max_length=3)
    id_number: str | None = Field(default=None, max_length=100)
    # Why the search was run, kept in the audit log with it.
    reason: str | None = Field(default=None, max_length=500)


class ConsoleAmlCandidate(BaseModel):
    """One listed entity the screening matched, flattened from yente's answer."""

    id: str
    name: str
    entity_type: str
    score: float
    # FollowTheMoney topics, such as sanction, role.pep, crime or poi.
    topics: list[str]
    datasets: list[str]
    aliases: list[str]
    birth_dates: list[str]
    nationalities: list[str]
    countries: list[str]
    positions: list[str]
    first_seen: str | None
    last_seen: str | None


class ConsoleAmlResult(BaseModel):
    # no_match, possible_match, strong_match or unavailable
    outcome: str
    action: str
    highest_score: float | None
    dataset: str
    screened_at: str
    error: str | None
    candidates: list[ConsoleAmlCandidate]


class ConsoleAmlQuery(BaseModel):
    full_name: str
    date_of_birth: str | None
    nationality: str | None
    id_number: str | None


class ConsoleAmlSearchResponse(BaseModel):
    """A search run from the console. `id` is the audit event that records it."""

    id: str
    query: ConsoleAmlQuery
    reason: str | None
    result: ConsoleAmlResult
    by: str
    at: datetime


class ConsoleAmlTopMatch(BaseModel):
    name: str
    score: float
    topics: list[str] = Field(default_factory=list)


class ConsoleAmlVerificationRef(BaseModel):
    id: str
    reference: str
    status: str
    client: ConsoleClient


class ConsoleAmlEntry(BaseModel):
    """One AML result in the console's list: a search run from the console, or
    the AML step of a verification. `id` is "search:<audit event id>" or
    "verification:<verification id>"."""

    id: str
    source: str  # manual or verification
    at: datetime
    # Who was screened: what was typed for a search, what the document read for a verification.
    person: ConsoleAmlQuery
    outcome: str
    highest_score: float | None
    match_count: int
    top_match: ConsoleAmlTopMatch | None
    # A search: who ran it and why.
    by: str | None = None
    reason: str | None = None
    # A verification: which one, its status and client.
    verification: ConsoleAmlVerificationRef | None = None
    # Every match. Missing for searches run before full results were kept.
    result: ConsoleAmlResult | None


class ConsoleAmlEntryList(BaseModel):
    items: list[ConsoleAmlEntry]
    total: int
    page: int
    page_size: int
    # Under the source and text filters, but not the outcome one.
    outcome_counts: dict[str, int]
    # Under the outcome and text filters, but not the source one.
    source_counts: dict[str, int]
    # The scores at which a match becomes possible and strong.
    thresholds: dict[str, float]


# The audit log: what staff did in the console, and when.

class ConsoleAuditEvent(BaseModel):
    id: str
    event_type: str
    # "system", or the console account that did it.
    actor: str
    actor_name: str | None
    actor_email: str | None
    verification_id: str | None
    payload: dict
    created_at: datetime


class ConsoleAuditEventList(BaseModel):
    items: list[ConsoleAuditEvent]
    total: int
    page: int
    page_size: int
    # How many match the other filters, for each category (the part of the
    # event type before the dot: aml, api_key, console, due_diligence_form).
    category_counts: dict[str, int]


# Metrics for the console's Overview and Analytics pages. Every window is whole
# UTC days, both ends included, and is compared with the window of the same
# length just before it. Rates are 0 to 1, or null when nothing was decided.

class ConsoleMetricRange(BaseModel):
    from_: date = Field(serialization_alias="from")
    to: date
    days: int


class ConsoleMetricValue(BaseModel):
    value: float | None
    previous: float | None


class ConsoleMetricPoint(BaseModel):
    # The first day of the bucket: a day, or a week starting on that day.
    date: date
    verifications: int
    passed: int
    failed: int
    pass_rate: float | None


class ConsoleMetricSeries(BaseModel):
    granularity: str  # day or week
    points: list[ConsoleMetricPoint]


class ConsoleMetricCount(BaseModel):
    key: str
    label: str | None = None
    count: int
    share: float | None = None
    pass_rate: float | None = None


class ConsoleAwaitingReview(BaseModel):
    count: int
    # When the longest-waiting one finished its session, if any is waiting.
    oldest_at: datetime | None


class ConsoleOverviewMetrics(BaseModel):
    range: ConsoleMetricRange
    verifications: ConsoleMetricValue
    pass_rate: ConsoleMetricValue
    # Median seconds from the session opening to its decision.
    completion_seconds: ConsoleMetricValue
    # Right now, not in the window.
    awaiting_review: ConsoleAwaitingReview
    series: ConsoleMetricSeries
    # By status as a client would see it now, for verifications created in the window.
    outcomes: list[ConsoleMetricCount]
    # The commonest reasons on failed verifications, most first.
    decline_reasons: list[ConsoleMetricCount]


class ConsoleStepTime(BaseModel):
    step: str
    median_seconds: float
    count: int


class ConsoleAnalyticsMetrics(BaseModel):
    range: ConsoleMetricRange
    # Reached a decision (passed, failed or needs review) out of every verification created.
    completion_rate: ConsoleMetricValue
    # Sent to review out of every verification created.
    review_rate: ConsoleMetricValue
    # Attempts per step taken, retakes included: 1.0 means nobody retook anything.
    attempts_per_step: ConsoleMetricValue
    series: ConsoleMetricSeries
    countries: list[ConsoleMetricCount]
    documents: list[ConsoleMetricCount]
    step_times: list[ConsoleStepTime]
    clients: list[ConsoleMetricCount]
    # mobile, desktop: from the browser that sent the document and face steps.
    devices: list[ConsoleMetricCount]


# Review decisions: what staff do with a verification the checks could not decide.

class ConsoleReviewDecisionRequest(BaseModel):
    # Why, kept in the audit log. Required to decline, optional to approve.
    note: str | None = Field(default=None, max_length=1000)


class ConsoleRedoRequest(BaseModel):
    """Steps the person has to do again on the same link."""

    steps: list[str] = Field(min_length=1)
    note: str | None = Field(default=None, max_length=1000)
    # How long the reopened link works for.
    hours: int = Field(default=72, ge=1, le=24 * 30)


class ConsoleReviewEvent(BaseModel):
    action: str  # approved, declined or redo_requested
    by: str
    note: str | None
    steps: list[str]
    at: datetime


class ConsoleReviewResult(BaseModel):
    status: str
    event: str | None
    # Set when a redo was requested: the link to send the person.
    session_url: str | None = None
    expires_at: datetime | None = None
    review: ConsoleReviewEvent


ConsoleVerificationDetail.model_rebuild()
