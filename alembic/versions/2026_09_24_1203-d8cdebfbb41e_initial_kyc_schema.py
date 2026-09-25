"""initial kyc schema

Revision ID: d8cdebfbb41e
Revises:
Create Date: 2026-09-24 12:03:47.251326

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d8cdebfbb41e"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The enum types are created and dropped here by hand, and the columns use them
# with create_type=False. Left to autogenerate, CREATE TYPE happens as a side
# effect of the first column that mentions the type and nothing ever drops it,
# so downgrade leaves the types behind and the next upgrade fails on a type that
# already exists.
verification_status = postgresql.ENUM(
    "pending",
    "in_progress",
    "passed",
    "failed",
    "needs_review",
    "expired",
    name="verification_status",
    create_type=False,
)
document_type = postgresql.ENUM(
    "passport", "id_card", "driving_license", name="document_type", create_type=False
)
document_decision = postgresql.ENUM(
    "pass", "fail", "review", "retake", name="document_decision", create_type=False
)
image_side = postgresql.ENUM("front", "back", name="image_side", create_type=False)

ENUM_TYPES = (verification_status, document_type, document_decision, image_side)


def upgrade() -> None:
    bind = op.get_bind()
    for enum_type in ENUM_TYPES:
        enum_type.create(bind)

    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=True),
        sa.Column("first_name", sa.String(), nullable=True),
        sa.Column("last_name", sa.String(), nullable=True),
        sa.Column("date_of_birth", sa.Date(), nullable=True),
        sa.Column("country", sa.String(length=3), nullable=True),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("phone", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    op.create_index(op.f("ix_users_external_id"), "users", ["external_id"], unique=True)

    op.create_table(
        "verification_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("status", verification_status, server_default="pending", nullable=False),
        sa.Column(
            "reason_codes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("client_reference", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_verification_sessions_user_id"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_verification_sessions")),
    )
    op.create_index(
        op.f("ix_verification_sessions_client_reference"),
        "verification_sessions",
        ["client_reference"],
        unique=False,
    )
    op.create_index(
        op.f("ix_verification_sessions_status"), "verification_sessions", ["status"], unique=False
    )
    op.create_index(
        op.f("ix_verification_sessions_user_id"), "verification_sessions", ["user_id"], unique=False
    )

    op.create_table(
        "document_scans",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("document_type", document_type, nullable=False),
        sa.Column("decision", document_decision, nullable=False),
        sa.Column(
            "reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column("document_type_code", sa.String(), nullable=True),
        sa.Column("document_number", sa.String(), nullable=True),
        sa.Column("issuing_country", sa.String(length=3), nullable=True),
        sa.Column("nationality", sa.String(length=3), nullable=True),
        sa.Column("surname", sa.String(), nullable=True),
        sa.Column("given_names", sa.String(), nullable=True),
        sa.Column("full_name", sa.String(), nullable=True),
        sa.Column("date_of_birth", sa.Date(), nullable=True),
        sa.Column("issue_date", sa.Date(), nullable=True),
        sa.Column("expiry_date", sa.Date(), nullable=True),
        sa.Column("sex", sa.String(length=1), nullable=True),
        sa.Column(
            "checks",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("processing_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["verification_sessions.id"],
            name=op.f("fk_document_scans_session_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_document_scans_user_id"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_scans")),
    )
    op.create_index(op.f("ix_document_scans_session_id"), "document_scans", ["session_id"], unique=False)
    op.create_index(op.f("ix_document_scans_user_id"), "document_scans", ["user_id"], unique=False)

    op.create_table(
        "document_images",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_scan_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("side", image_side, nullable=False),
        sa.Column("storage_key", sa.String(), nullable=False),
        sa.Column("content_type", sa.String(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_scan_id"],
            ["document_scans.id"],
            name=op.f("fk_document_images_document_scan_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["verification_sessions.id"],
            name=op.f("fk_document_images_session_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_images")),
        sa.UniqueConstraint("document_scan_id", "side", name=op.f("uq_document_images_document_scan_id_side")),
    )
    op.create_index(
        op.f("ix_document_images_document_scan_id"), "document_images", ["document_scan_id"], unique=False
    )
    op.create_index(op.f("ix_document_images_session_id"), "document_images", ["session_id"], unique=False)
    op.create_index(op.f("ix_document_images_sha256"), "document_images", ["sha256"], unique=False)

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("actor", sa.String(), server_default="system", nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["verification_sessions.id"],
            name=op.f("fk_audit_events_session_id"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_audit_events_user_id"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    op.create_index(op.f("ix_audit_events_event_type"), "audit_events", ["event_type"], unique=False)
    op.create_index(op.f("ix_audit_events_session_id"), "audit_events", ["session_id"], unique=False)
    op.create_index(op.f("ix_audit_events_user_id"), "audit_events", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("document_images")
    op.drop_table("document_scans")
    op.drop_table("verification_sessions")
    op.drop_table("users")

    bind = op.get_bind()
    for enum_type in ENUM_TYPES:
        enum_type.drop(bind)
