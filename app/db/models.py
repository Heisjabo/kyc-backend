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
import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAt, Timestamp, UpdatedAt, UuidPk
from app.schemas.document import Decision, DocumentType


class VerificationStatus(str, enum.Enum):
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

    id: Mapped[UuidPk]
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

    id: Mapped[UuidPk]
    account_id: Mapped[uuid.UUID] = mapped_column(
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
    created_at: Mapped[CreatedAt]

    account: Mapped["Account"] = relationship(back_populates="api_keys")


class PasswordReset(Base):
    """A single-use token for setting a forgotten password.

    Only the hash is stored: somebody who reads this table cannot use what they
    find to take over an account. The row survives its use so that a reset can be
    accounted for afterwards.
    """

    __tablename__ = "password_resets"

    id: Mapped[UuidPk]
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[Timestamp] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[Timestamp | None]
    created_at: Mapped[CreatedAt]


class User(Base):
    __tablename__ = "users"

    id: Mapped[UuidPk]
    # The id this person has in the system that asked for the check. Unique, but
    # nullable: a walk-in verification has no caller-side identity yet.
    external_id: Mapped[str | None] = mapped_column(String, unique=True, index=True)
    # Identity fields fill in as the checks run, so none of them can be required
    # at insert: a session may open with nothing but a reference string.
    first_name: Mapped[str | None] = mapped_column(String)
    last_name: Mapped[str | None] = mapped_column(String)
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    country: Mapped[str | None] = mapped_column(String(3))
    email: Mapped[str | None] = mapped_column(String)
    phone: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]

    sessions: Mapped[list["VerificationSession"]] = relationship(back_populates="user")


class VerificationSession(Base):
    __tablename__ = "verification_sessions"

    id: Mapped[UuidPk]
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    status: Mapped[VerificationStatus] = mapped_column(
        VERIFICATION_STATUS, nullable=False, server_default=VerificationStatus.pending.value, index=True
    )
    # The reason codes behind the session's verdict, as the API returns them.
    # The per-step reasons stay on the scan that produced them.
    reason_codes: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=_EMPTY_LIST)
    # The caller's own handle for this check, echoed back to them and used to
    # find the session again when their side is the only one holding an id.
    client_reference: Mapped[str | None] = mapped_column(String, index=True)
    started_at: Mapped[Timestamp | None]
    completed_at: Mapped[Timestamp | None]
    expires_at: Mapped[Timestamp | None]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]

    user: Mapped["User | None"] = relationship(back_populates="sessions")
    document_scans: Mapped[list["DocumentScan"]] = relationship(back_populates="session")


class DocumentScan(Base):
    __tablename__ = "document_scans"

    id: Mapped[UuidPk]
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("verification_sessions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # Denormalised from the session so scans can be found by person without
    # joining, and so a scan keeps pointing at whoever it was attributed to.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    document_type: Mapped[DocumentType] = mapped_column(DOCUMENT_TYPE, nullable=False)
    decision: Mapped[Decision] = mapped_column(DOCUMENT_DECISION, nullable=False)
    reasons: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=_EMPTY_LIST)
    warnings: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=_EMPTY_LIST)

    # What the document said. "mrz" when read from the machine readable zone,
    # "visual" when read from the printed text; the rest is whatever that
    # source yielded, which on a poor scan is very little.
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

    # Every check the pipeline ran, with its status, reasons and measurements.
    # Kept whole rather than split into rows: the set of checks changes with the
    # rules, and this is read back as one blob to explain a decision.
    checks: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=_EMPTY_LIST)
    processing_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[CreatedAt]

    session: Mapped["VerificationSession"] = relationship(back_populates="document_scans")
    images: Mapped[list["DocumentImage"]] = relationship(
        back_populates="document_scan", passive_deletes=True
    )


class DocumentImage(Base):
    __tablename__ = "document_images"
    # A scan has one front and at most one back. A second upload of a side is a
    # new scan, not a second row here.
    __table_args__ = (UniqueConstraint("document_scan_id", "side"),)

    id: Mapped[UuidPk]
    document_scan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_scans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("verification_sessions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    side: Mapped[ImageSide] = mapped_column(IMAGE_SIDE, nullable=False)
    # Where the file is, not what it holds: these are photographs of identity
    # documents and they belong in object storage, which comes later. Until
    # then this is the path the bytes were written to.
    storage_key: Mapped[str] = mapped_column(String, nullable=False)
    content_type: Mapped[str] = mapped_column(String, nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    # Hex SHA-256 of the bytes: proves the stored file is the one that was
    # judged, and finds the same document submitted twice.
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # When the camera took it, which is not when we received it.
    captured_at: Mapped[Timestamp | None]
    created_at: Mapped[CreatedAt]

    document_scan: Mapped["DocumentScan"] = relationship(back_populates="images")


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[UuidPk]
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("verification_sessions.id", ondelete="SET NULL"), index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    event_type: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # "system" for anything the pipeline decided on its own, otherwise the id of
    # the operator who did it.
    actor: Mapped[str] = mapped_column(String, nullable=False, server_default="system")
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=_EMPTY_OBJECT)
    created_at: Mapped[CreatedAt]


# PLACEHOLDER: face and liveness tables come later.
#
# Nothing above stores biometric data: no selfie frames, no face embeddings, no
# liveness or match results. That is deliberate, not an oversight. The face
# pipeline's thresholds are still inherited rather than calibrated (see README),
# and storing faces raises retention questions that have to be answered before,
# not after, the first row is written.
