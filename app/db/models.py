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
