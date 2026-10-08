"""console invites

Revision ID: d7e4b1c09a32
Revises: c3d9a2f61e85
Create Date: 2026-10-08

console_invites holds invitations to the admin console. Each is accepted
through a one-time link; only a hash of the link's token is stored.

Additive: one new table.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d7e4b1c09a32"
down_revision = "c3d9a2f61e85"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "console_invites",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("first_name", sa.String(length=80)),
        sa.Column("last_name", sa.String(length=80)),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("products", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("token_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("invited_by_id", sa.BigInteger(), sa.ForeignKey("accounts.id", ondelete="SET NULL")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("accounts.id", ondelete="SET NULL")),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_console_invites_email", "console_invites", ["email"])


def downgrade() -> None:
    op.drop_index("ix_console_invites_email", table_name="console_invites")
    op.drop_table("console_invites")
