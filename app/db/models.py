"""The tables behind a verification.

A scan currently returns its verdict and forgets it (app/storage/sessions.py).
These tables are where that verdict and the evidence behind it are kept, so a
decision can still be explained, and re-checked, long after the request that
made it.

Deletion is deliberate, because evidence outliving its context is worse than
useless: a session cannot be deleted while scans point at it (RESTRICT), and a
scan takes its images with it (CASCADE), so no image ever outlives the scan it
documents. Audit events are the exception: their links drop to NULL (SET NULL)
rather than holding the row hostage, because a trail that vanishes with the
thing it records is not a trail.

Enum columns store the enum's *value*, not its Python member name, so the
database reads the way the API does ("pass", not "passed"). DocumentType and
Decision are the API's own enums: one definition, so a new decision cannot
appear in a response without also failing to insert here. Adding a member to
either needs a migration that alters the Postgres type.
"""

import enum
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAt, IntPk, Timestamp, UpdatedAt
from app.schemas.document import Decision, DocumentType


class VerificationStatus(str, enum.Enum):
    """Where a verification stands.

    The vocabulary follows Shufti's events, because a client already integrated
    with them should be able to point at us with almost no change: their
    request.pending, verification.accepted, verification.declined and so on map
    onto these one for one.
    """

    pending = "pending"
    in_progress = "in_progress"
    passed = "passed"
    failed = "failed"
    needs_review = "needs_review"
    expired = "expired"


class ImageSide(str, enum.Enum):
    front = "front"
    back = "back"


def _pg_enum(python_enum: type[enum.Enum], name: str) -> Enum:
    return Enum(python_enum, name=name, values_callable=lambda members: [m.value for m in members])


VERIFICATION_STATUS = _pg_enum(VerificationStatus, "verification_status")
DOCUMENT_TYPE = _pg_enum(DocumentType, "document_type")
DOCUMENT_DECISION = _pg_enum(Decision, "document_decision")
IMAGE_SIDE = _pg_enum(ImageSide, "image_side")

_EMPTY_LIST = text("'[]'::jsonb")
_EMPTY_OBJECT = text("'{}'::jsonb")

# The admin console's roles and products, the same ones pangolin-admin shows on
# its Team & roles page. Kept as strings rather than a Postgres enum so a role can
# be added without a migration.
CONSOLE_ROLES = ("owner", "admin", "reviewer", "support", "readonly")
CONSOLE_PRODUCTS = ("kyc", "sign")


class Account(Base):
    """A customer of this service: the person or organisation calling the API.

    Not to be confused with User below, which is a person being *verified*. An
    account holds API keys and, later, the verifications made under them.

    The fields follow tuesday-backend's signup, minus the parts that belong to
    its trading flow (role, amount, stock) and to its entity path (company
    representatives), so a person signing up here is asked what they are already
    asked over there.
    """

    __tablename__ = "accounts"

    @property
    def token_version(self) -> str:
        """Sign-in tokens carry this. It comes from the stored password hash, so a password
        reset or change gives a new value and earlier sessions stop working. Nothing extra
        is stored, so no database change is needed."""
        import hashlib

        return hashlib.sha256((self.password_hash or "").encode()).hexdigest()[:16]

    id: Mapped[IntPk]
    # Stored lower-cased, so "Jean@X.com" and "jean@x.com" are one account.
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    # Argon2id. The plain password is never held longer than the request.
    password_hash: Mapped[str] = mapped_column(String, nullable=False)

    first_name: Mapped[str] = mapped_column(String, nullable=False)
    last_name: Mapped[str] = mapped_column(String, nullable=False)
    # Kept apart from the number itself, the way tuesday-backend keeps it: the
    # same number reads differently under a different dialling code.
    country_code: Mapped[str] = mapped_column(String(8), nullable=False)
    phone: Mapped[str] = mapped_column(String(32), nullable=False)
    date_of_birth: Mapped[date] = mapped_column(Date, nullable=False)
    country: Mapped[str] = mapped_column(String, nullable=False)

    # Asked for, and kept when given, but not required: an account is usable
    # without them and refusing one for a missing city helps nobody.
    gender: Mapped[str | None] = mapped_column(String(32))
    city: Mapped[str | None] = mapped_column(String)
    address: Mapped[str | None] = mapped_column(String)

    # A suspended account keeps its keys and its history; they simply stop working.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    # Set only for Pangolin staff who sign in to the admin console (pangolin-admin).
    # Null for every client account, which is what keeps a client out of it. The
    # role is one of CONSOLE_ROLES; the products are the ones this person may open
    # there ("kyc", "sign").
    console_role: Mapped[str | None] = mapped_column(String(16))
    console_products: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=_EMPTY_LIST)

    # Billing. The plan decides which features the account may use
    # (app/core/plans.py); what it has left to spend is in credit_grants.
    plan: Mapped[str] = mapped_column(String(16), nullable=False, server_default="free")
    plan_changed_at: Mapped[Timestamp | None]
    # What one unit costs this client once its free credit is spent, in US
    # cents: $1 unless an Enterprise rate has been agreed.
    unit_price_cents: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))
    # Lets an invoiced Enterprise account keep verifying once its credit is
    # spent. What it uses past zero is still metered, for the invoice.
    allow_overdraft: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    last_login_at: Mapped[Timestamp | None]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]

    api_keys: Mapped[list["ApiKey"]] = relationship(back_populates="account", passive_deletes=True)


class ApiKey(Base):
    """A key that authorises calls made on an account's behalf.

    The key itself is shown once, at creation, and never again: only its hash is
    kept, so a copy of this table is not a set of working credentials. The prefix
    is stored in the clear because it is how a presented key is found, and it is
    what a person recognises in a list of keys.

    Keys expire. A key that lives forever is one nobody ever rotates.
    """

    __tablename__ = "api_keys"

    id: Mapped[IntPk]
    account_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # What this key is for, in the owner's words: "production", "staging tests".
    name: Mapped[str] = mapped_column(String, nullable=False)
    prefix: Mapped[str] = mapped_column(String(16), unique=True, nullable=False, index=True)
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[Timestamp] = mapped_column(DateTime(timezone=True), nullable=False)
    # Written on use, so a key nobody calls can be found and removed.
    last_used_at: Mapped[Timestamp | None]
    revoked_at: Mapped[Timestamp | None]
    # Set by Pangolin staff in the admin console to stop a key for now. Unlike
    # revoking, it can be undone: clearing it lets the key work again.
    suspended_at: Mapped[Timestamp | None]
    # Set when the owner deletes the key: it is revoked first, then left out of
    # the owner's list. The row stays, so the requests made with it and the
    # audit trail still name it.
    deleted_at: Mapped[Timestamp | None]
    created_at: Mapped[CreatedAt]

    account: Mapped["Account"] = relationship(back_populates="api_keys")


class PasswordReset(Base):
    """A single-use token for setting a forgotten password.

    Only the hash is stored: somebody who reads this table cannot use what they
    find to take over an account. The row survives its use so that a reset can be
    accounted for afterwards.
    """

    __tablename__ = "password_resets"

    id: Mapped[IntPk]
    account_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[Timestamp] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[Timestamp | None]
    created_at: Mapped[CreatedAt]


class VerificationStep(str, enum.Enum):
    """The steps a verification can be made of.

    All of them exist from the start, even the ones not implemented yet, because
    adding a step later means a migration and a change to a vocabulary clients
    have already written code against.
    """

    document = "document"
    face = "face"
    address = "address"
    due_diligence = "due_diligence"
    aml = "aml"
    consent = "consent"


class StepStatus(str, enum.Enum):
    pending = "pending"
    passed = "pass"
    failed = "fail"
    review = "review"
    skipped = "skipped"


class FileKind(str, enum.Enum):
    """Every artefact a verification produces, so the report can be assembled
    from rows rather than from whatever happened to be kept."""

    document_front = "document_front"
    document_back = "document_back"
    # The face cut out of the document, kept apart from the selfie so a report
    # can put the two side by side without guessing which is which.
    document_portrait = "document_portrait"
    address_proof = "address_proof"
    # A file the person attached to a due diligence answer: a bank statement, a
    # portfolio summary, a letter showing where the money comes from.
    due_diligence_document = "due_diligence_document"
    portrait = "portrait"
    challenge_video = "challenge_video"
    report_pdf = "report_pdf"


VERIFICATION_STEP = _pg_enum(VerificationStep, "verification_step")
STEP_STATUS = _pg_enum(StepStatus, "step_status")
FILE_KIND = _pg_enum(FileKind, "file_kind")


class Applicant(Base):
    """A person being verified.

    Not an Account, which is the customer of this service: an applicant never
    signs in. Shufti keeps the same distinction and shows it as the Customer ID
    on their report, beside the per-request Reference ID.

    Applicants belong to one account. The same human verified by two different
    clients is two applicants, because sharing identity between customers is a
    legal question rather than a technical one.
    """

    __tablename__ = "applicants"

    id: Mapped[IntPk]
    account_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # What we show and accept as this person's id, the way Shufti shows a
    # Customer ID. Generated here, short enough to read out over a phone.
    customer_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    # The client's own id for this person, when they send one. Without it, a
    # returning person is a new applicant and the duplicate face check will say
    # so, which is noise the client can avoid by sending it.
    client_customer_id: Mapped[str | None] = mapped_column(String, index=True)

    # What the documents proved, not what anyone typed. The claimed values live
    # on the verification that claimed them.
    first_name: Mapped[str | None] = mapped_column(String)
    last_name: Mapped[str | None] = mapped_column(String)
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    country: Mapped[str | None] = mapped_column(String(3))
    email: Mapped[str | None] = mapped_column(String)
    phone: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]

    verifications: Mapped[list["Verification"]] = relationship(back_populates="applicant")
    # The client this applicant belongs to, for reading only (the admin console lists it).
    account: Mapped["Account"] = relationship(viewonly=True)


class Verification(Base):
    """One verification: created the moment a client initiates it.

    A verification is a session a person walks through, not a single check. It
    holds what was asked for, where it stands, and how it ended; the steps hold
    what happened at each stage.
    """

    __tablename__ = "verifications"
    __table_args__ = (UniqueConstraint("account_id", "reference"),)

    id: Mapped[IntPk]
    account_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    applicant_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("applicants.id", ondelete="RESTRICT"), index=True
    )
    # A child is verified on the back of an adult who was verified first: their
    # certificate proves the link rather than an identity, so the check is only
    # worth as much as the parent's verification it hangs off.
    parent_verification_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("verifications.id", ondelete="RESTRICT"), index=True
    )

    # The client's own string for this verification, sent when they initiate it
    # and used by them to look up how their customer got on. Unique within the
    # account, as Shufti requires it to be: two verifications cannot share one.
    # Generated here when a client does not send one, so there is always a key.
    reference: Mapped[str] = mapped_column(String(250), nullable=False, index=True)

    # The session the person opens. The token is what the URL is built from, so
    # it is unguessable and can be rotated without changing the reference. The
    # same URL works on a phone, which is what makes the QR handoff possible.
    session_token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    session_url: Mapped[str] = mapped_column(String, nullable=False)

    status: Mapped[VerificationStatus] = mapped_column(
        VERIFICATION_STATUS, nullable=False, server_default=VerificationStatus.pending.value, index=True
    )
    # Shufti's event vocabulary, for clients that already read it.
    event: Mapped[str | None] = mapped_column(String(64))

    # Which steps this verification is made of, in the order they are asked for.
    services: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=_EMPTY_LIST)
    form_id: Mapped[str | None] = mapped_column(String, index=True)

    callback_url: Mapped[str | None] = mapped_column(String)
    redirect_url: Mapped[str | None] = mapped_column(String)
    country: Mapped[str | None] = mapped_column(String(3))
    language: Mapped[str | None] = mapped_column(String(8))
    verification_mode: Mapped[str | None] = mapped_column(String(16))

    # What the client claimed about this person when they opened the session.
    # Kept apart from the applicant's verified fields on purpose: one is a
    # claim, the other is what a document proved.
    claimed: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=_EMPTY_OBJECT)
    # Everything the client asked for, echoed back in the report the way Shufti
    # echoes its Verification Settings block.
    settings: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=_EMPTY_OBJECT)

    reasons: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=_EMPTY_LIST)
    declined_codes: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=_EMPTY_LIST)
    warnings: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=_EMPTY_LIST)

    expires_at: Mapped[Timestamp | None]
    started_at: Mapped[Timestamp | None]
    completed_at: Mapped[Timestamp | None]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]

    applicant: Mapped["Applicant | None"] = relationship(back_populates="verifications")
    # The client the verification belongs to. Read-only from this side: nothing
    # reaches verifications through Account.
    account: Mapped["Account"] = relationship(viewonly=True)
    parent_verification: Mapped["Verification | None"] = relationship(
        remote_side="Verification.id", foreign_keys=[parent_verification_id]
    )
    steps: Mapped[list["Step"]] = relationship(back_populates="verification", passive_deletes=True)
    files: Mapped[list["VerificationFile"]] = relationship(
        back_populates="verification", passive_deletes=True
    )


class Step(Base):
    """One attempt at one step of a verification.

    A retry is a new row, not an overwrite, so a person who failed twice on
    glare and passed on the third try reads that way afterwards.
    """

    __tablename__ = "verification_steps"
    __table_args__ = (UniqueConstraint("verification_id", "step", "attempt"),)

    id: Mapped[IntPk]
    verification_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("verifications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    step: Mapped[VerificationStep] = mapped_column(VERIFICATION_STEP, nullable=False, index=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    status: Mapped[StepStatus] = mapped_column(
        STEP_STATUS, nullable=False, server_default=StepStatus.pending.value, index=True
    )

    reasons: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=_EMPTY_LIST)
    warnings: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=_EMPTY_LIST)
    # The whole answer this step produced, checks and measurements included.
    result: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=_EMPTY_OBJECT)
    # What this step read: document fields, form answers, an address.
    extracted: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=_EMPTY_OBJECT)
    # Which device did this step, so a report can say the document was captured
    # on a phone while the rest happened on a laptop.
    device: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=_EMPTY_OBJECT)

    # Did any image this step received look generated? True, false, or null for a
    # step that could not look: no image large enough to score, or the detector not
    # installed. A step can carry several images (a front and a back, a bill and its
    # second page, the frames of a face check), and this is true when any of them
    # scored at or above AI_IMAGE_DETECT_THRESHOLD.
    #
    # It never changes the step's status. The score and the per-image scores are in
    # `result`, under the ai_generated check.
    ai_generated: Mapped[bool | None] = mapped_column(Boolean)

    started_at: Mapped[Timestamp | None]
    completed_at: Mapped[Timestamp | None]
    created_at: Mapped[CreatedAt]

    verification: Mapped["Verification"] = relationship(back_populates="steps")
    document_scans: Mapped[list["DocumentScan"]] = relationship(back_populates="step")
    files: Mapped[list["VerificationFile"]] = relationship(back_populates="step")


class DocumentScan(Base):
    """One document read: the verdict, and the fields it gave up."""

    __tablename__ = "document_scans"

    id: Mapped[IntPk]
    step_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("verification_steps.id", ondelete="CASCADE"), nullable=False, index=True
    )
    verification_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("verifications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Denormalised so scans can be found by person without joining.
    applicant_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("applicants.id", ondelete="RESTRICT"), index=True
    )
    document_type: Mapped[DocumentType] = mapped_column(DOCUMENT_TYPE, nullable=False)
    decision: Mapped[Decision] = mapped_column(DOCUMENT_DECISION, nullable=False)
    reasons: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=_EMPTY_LIST)
    warnings: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=_EMPTY_LIST)

    # What the document said. "mrz" when read from the machine readable zone,
    # "visual" when read from the printed text.
    source: Mapped[str | None] = mapped_column(String)
    document_type_code: Mapped[str | None] = mapped_column(String)
    document_number: Mapped[str | None] = mapped_column(String)
    issuing_country: Mapped[str | None] = mapped_column(String(3))
    nationality: Mapped[str | None] = mapped_column(String(3))
    surname: Mapped[str | None] = mapped_column(String)
    given_names: Mapped[str | None] = mapped_column(String)
    full_name: Mapped[str | None] = mapped_column(String)
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    issue_date: Mapped[date | None] = mapped_column(Date)
    expiry_date: Mapped[date | None] = mapped_column(Date)
    sex: Mapped[str | None] = mapped_column(String(1))

    checks: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=_EMPTY_LIST)
    processing_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[CreatedAt]

    step: Mapped["Step"] = relationship(back_populates="document_scans")


class VerificationFile(Base):
    """Every image and clip a verification produced.

    One table rather than one per kind, because the report reads them together
    and because a new kind of evidence should not need a new table. The bytes
    live in object storage; this row says where, and proves which bytes they
    were.
    """

    __tablename__ = "verification_files"

    id: Mapped[IntPk]
    verification_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("verifications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    step_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("verification_steps.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[FileKind] = mapped_column(FILE_KIND, nullable=False, index=True)
    storage_key: Mapped[str] = mapped_column(String, nullable=False)
    content_type: Mapped[str] = mapped_column(String, nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    # Hex SHA-256: proves the stored file is the one that was judged.
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # When the camera took it, which is not when we received it.
    captured_at: Mapped[Timestamp | None]
    created_at: Mapped[CreatedAt]

    verification: Mapped["Verification"] = relationship(back_populates="files")
    step: Mapped["Step | None"] = relationship(back_populates="files")


class FaceCheck(Base):
    """What the face step measured: liveness, the match, and the two warnings
    that are not failures."""

    __tablename__ = "face_checks"

    id: Mapped[IntPk]
    step_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("verification_steps.id", ondelete="CASCADE"), nullable=False, index=True
    )
    verification_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("verifications.id", ondelete="CASCADE"), nullable=False, index=True
    )

    liveness_passed: Mapped[bool | None] = mapped_column(Boolean)
    challenge_completed: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=_EMPTY_LIST)
    # How far the moving face was from flat, as a percentage of face size. A
    # printed photograph scores near zero.
    planarity: Mapped[float | None] = mapped_column(Float)

    # Not implemented yet, and the columns exist so that turning it on is a
    # write rather than a migration.
    deepfake_checked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    deepfake_passed: Mapped[bool | None] = mapped_column(Boolean)
    deepfake_score: Mapped[float | None] = mapped_column(Float)

    match_score: Mapped[float | None] = mapped_column(Float)
    match_threshold: Mapped[float | None] = mapped_column(Float)
    matched: Mapped[bool | None] = mapped_column(Boolean)

    # The duplicate face warning: this face already belongs to somebody else's
    # verification. A warning, never a failure, and it names where it was seen,
    # the way Shufti's duplication block links to the other reference.
    duplicate_of_verification_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("verifications.id", ondelete="SET NULL"), index=True
    )
    duplicate_score: Mapped[float | None] = mapped_column(Float)

    result: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=_EMPTY_OBJECT)
    created_at: Mapped[CreatedAt]


class FaceTemplate(Base):
    """The numbers a face reduces to, kept so a later verification can ask
    whether it has seen this face before.

    Stored as an array rather than a vector column because pgvector is not
    installed here; the comparison is done in the application. Swapping to
    pgvector later changes this column and nothing else.
    """

    __tablename__ = "face_templates"

    id: Mapped[IntPk]
    account_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    applicant_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("applicants.id", ondelete="CASCADE"), index=True
    )
    verification_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("verifications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # "portrait" (the still taken at the camera) or "document" (the printed one).
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    embedding: Mapped[list] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[CreatedAt]


class DeviceSession(Base):
    """Where the person was and what they were using.

    Shufti's report calls this Device and Network Intelligence, and it is the
    difference between "the check passed" and "the check passed from a data
    centre in another country through a proxy". The risk fields are nullable
    because they arrive from a lookup we have not wired up yet.
    """

    __tablename__ = "device_sessions"

    id: Mapped[IntPk]
    verification_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("verifications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ip: Mapped[str | None] = mapped_column(String(64), index=True)
    country: Mapped[str | None] = mapped_column(String(64))
    city: Mapped[str | None] = mapped_column(String(120))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    asn: Mapped[str | None] = mapped_column(String(32))
    asn_name: Mapped[str | None] = mapped_column(String(160))
    # A browser fingerprint, so two sessions from one machine can be linked even
    # when the address changes.
    fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    timezone: Mapped[str | None] = mapped_column(String(64))
    operating_system: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String)

    is_proxy: Mapped[bool | None] = mapped_column(Boolean)
    is_tor: Mapped[bool | None] = mapped_column(Boolean)
    is_datacenter: Mapped[bool | None] = mapped_column(Boolean)
    country_mismatch: Mapped[bool | None] = mapped_column(Boolean)
    timezone_mismatch: Mapped[bool | None] = mapped_column(Boolean)
    emulated_device: Mapped[bool | None] = mapped_column(Boolean)
    rooted_device: Mapped[bool | None] = mapped_column(Boolean)
    threat_level: Mapped[str | None] = mapped_column(String(16))
    risk_level: Mapped[str | None] = mapped_column(String(16))
    fraud_score: Mapped[int | None] = mapped_column(Integer)
    signals: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=_EMPTY_OBJECT)

    first_seen_at: Mapped[CreatedAt]
    last_seen_at: Mapped[UpdatedAt]


class CallbackDelivery(Base):
    """Every attempt to tell the client what happened.

    Shufti's report shows this as a table of event, attempts, HTTP code and
    timestamp, and it is the first thing anyone asks for when a client says they
    were never told.
    """

    __tablename__ = "callback_deliveries"

    id: Mapped[IntPk]
    verification_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("verifications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    url: Mapped[str] = mapped_column(String, nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[str | None] = mapped_column(String)
    error: Mapped[str | None] = mapped_column(String)
    sent_at: Mapped[CreatedAt]


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[IntPk]
    verification_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("verifications.id", ondelete="SET NULL"), index=True
    )
    applicant_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("applicants.id", ondelete="SET NULL"), index=True
    )
    event_type: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # "system" for anything the pipeline decided on its own, otherwise the id of
    # the operator who did it.
    actor: Mapped[str] = mapped_column(String, nullable=False, server_default="system")
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=_EMPTY_OBJECT)
    created_at: Mapped[CreatedAt]


# A due diligence form is a draft while it is being written, active while
# clients can attach it to a verification, and archived once retired. Kept as a
# string rather than a Postgres enum, like the console roles.
FORM_STATUSES = ("draft", "active", "archived")


class DueDiligenceFormClient(Base):
    """Which clients may attach which form. A form nobody is given cannot be used."""

    __tablename__ = "due_diligence_form_clients"

    form_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("due_diligence_forms.id", ondelete="CASCADE"), primary_key=True
    )
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    created_at: Mapped[CreatedAt]


class DueDiligenceForm(Base):
    """The questions asked in the due diligence step, written in the admin console.

    `public_id` is what a client sends as `form_id` when opening a verification,
    the way Shufti's questionnaire uuid is sent. It never changes, so a client's
    integration keeps working while the questions behind it are edited.

    `fields` is the list of questions, each with a `key` that stays the same when
    its wording changes. A submitted step keeps a copy of the questions it was
    answered against, so editing a form never changes what an old answer meant.
    """

    __tablename__ = "due_diligence_forms"

    id: Mapped[IntPk]
    public_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="draft")
    fields: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=_EMPTY_LIST)
    created_by_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("accounts.id", ondelete="SET NULL")
    )
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]

    clients: Mapped[list["Account"]] = relationship(
        secondary="due_diligence_form_clients", order_by="Account.id"
    )


class ConsoleSetting(Base):
    """A platform setting changed in the admin console.

    One row per setting that has been changed; a setting without a row takes
    its default from the environment (app/core/config.py). Read through
    app/services/platform_settings.py, never directly.
    """

    __tablename__ = "console_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_by_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("accounts.id", ondelete="SET NULL")
    )
    updated_at: Mapped[UpdatedAt]


class ConsoleInvite(Base):
    """An invitation to join the admin console, accepted through a one-time link.

    There is no email yet, so whoever invites copies the link and sends it. Only
    a hash of the link's token is kept: the link is shown once, when it is made,
    and making a new one replaces it.
    """

    __tablename__ = "console_invites"

    id: Mapped[IntPk]
    email: Mapped[str] = mapped_column(String, nullable=False, index=True)
    first_name: Mapped[str | None] = mapped_column(String(80))
    last_name: Mapped[str | None] = mapped_column(String(80))
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    products: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=_EMPTY_LIST)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    invited_by_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("accounts.id", ondelete="SET NULL")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[Timestamp | None]
    account_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("accounts.id", ondelete="SET NULL")
    )
    revoked_at: Mapped[Timestamp | None]
    created_at: Mapped[CreatedAt]


# Where a grant of credit came from. Strings rather than a Postgres enum, like
# the console roles, so a new source needs no migration.
GRANT_SOURCES = ("free_grant", "top_up", "manual")
# What happened to one unit of usage: held when the work began, then either
# charged once it was decided or handed back when it never was.
USAGE_KINDS = ("reserve", "commit", "release")
TOP_UP_STATUSES = ("pending", "paid", "failed", "expired", "refunded")


class CreditGrant(Base):
    """A batch of units an account may spend: its free allowance, a top-up it
    paid for, or credit given by Pangolin staff.

    Counted in units (one verification, one Sign request), not money: free and
    paid credit then spend the same way, and an Enterprise rate is only a
    different price on the grant. `remaining` is the balance; it goes down when
    a unit is reserved and back up when a reservation is released.
    """

    __tablename__ = "credit_grants"
    # The free allowance is given once per product, however many requests race
    # to give it.
    __table_args__ = (
        Index(
            "uq_credit_grants_free_once",
            "account_id",
            "product",
            unique=True,
            postgresql_where=text("source = 'free_grant'"),
        ),
    )

    id: Mapped[IntPk]
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    remaining: Mapped[int] = mapped_column(Integer, nullable=False)
    # What one unit of this grant is worth, in US cents: 0 for free credit.
    unit_price_cents: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    top_up_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("top_ups.id", ondelete="RESTRICT"), unique=True
    )
    # Why staff gave it, for a manual grant.
    note: Mapped[str | None] = mapped_column(String(500))
    granted_by_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("accounts.id", ondelete="SET NULL"))
    created_at: Mapped[CreatedAt]


class UsageEvent(Base):
    """One movement of one unit, never changed once written.

    A verification reserves a unit when it is opened, commits it when it is
    decided, and releases it if it expires first. The unique keys make each of
    those happen at most once per verification (or per Sign request), so a
    retried request or a second settle can never charge twice.
    """

    __tablename__ = "usage_events"
    __table_args__ = (
        UniqueConstraint("verification_id", "kind"),
        UniqueConstraint("account_id", "product", "external_ref", "kind"),
    )

    id: Mapped[IntPk]
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    verification_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("verifications.id", ondelete="SET NULL"), index=True
    )
    # The Sign server's own id for a request: Sign requests are not rows here.
    external_ref: Mapped[str | None] = mapped_column(String(128))
    # The grant the unit came from. Null when the account had none left and was
    # let through anyway: overdraft, or billing not yet enforced.
    grant_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("credit_grants.id", ondelete="SET NULL"), index=True
    )
    unit_price_cents: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[CreatedAt]


class TopUp(Base):
    """A card payment for credit. Written when checkout starts; once the payment
    provider says it is paid, a CreditGrant for `units` is made from it.

    The payment provider is not wired up yet (Bachs is planned); the table is
    here so the grants that will hang off it have somewhere to point.
    """

    __tablename__ = "top_ups"

    id: Mapped[IntPk]
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    # Ours, sent to the provider so its callbacks can be matched back.
    reference: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    checkout_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    charge_id: Mapped[str | None] = mapped_column(String(128))
    units: Mapped[int] = mapped_column(Integer, nullable=False)
    # Always US dollars, in cents.
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending", index=True)
    # The provider's last word on it, kept whole for disputes.
    raw: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=_EMPTY_OBJECT)
    paid_at: Mapped[Timestamp | None]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]
