"""console settings

Revision ID: c3d9a2f61e85
Revises: b5e2c8d1f4a7
Create Date: 2026-10-08

console_settings holds the platform settings changed in the admin console,
one row per changed setting. A setting without a row keeps its default from
the environment, so an empty table changes nothing.

Additive: one new table.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c3d9a2f61e85"
down_revision = "b5e2c8d1f4a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "console_settings",
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_by_id", sa.BigInteger(), sa.ForeignKey("accounts.id", ondelete="SET NULL")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("console_settings")
