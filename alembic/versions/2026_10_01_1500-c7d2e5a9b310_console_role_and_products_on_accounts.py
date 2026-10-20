"""console role and products on accounts

Revision ID: c7d2e5a9b310
Revises: dc771820ad4a
Create Date: 2026-10-01

Staff sign in to the admin console with an account
like any other, marked by these two columns. console_role is null for every
client, which is what keeps clients out of the console; console_products lists
the products ("kyc") the person may open there.

Both are additive: existing accounts get a null role and an empty list, so no
client gains or loses anything.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "c7d2e5a9b310"
down_revision = "dc771820ad4a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("console_role", sa.String(length=16), nullable=True))
    op.add_column(
        "accounts",
        sa.Column("console_products", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
    )


def downgrade() -> None:
    op.drop_column("accounts", "console_products")
    op.drop_column("accounts", "console_role")
