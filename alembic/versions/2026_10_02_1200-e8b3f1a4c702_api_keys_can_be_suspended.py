"""api keys can be suspended

Revision ID: e8b3f1a4c702
Revises: c7d2e5a9b310
Create Date: 2026-10-02

Staff can suspend a client's API key from the admin console: the key
stops working until it is resumed. suspended_at holds when, and is null for a
key that is not suspended.

Additive: every existing key gets a null, so no key changes behaviour.
"""

import sqlalchemy as sa
from alembic import op

revision = "e8b3f1a4c702"
down_revision = "c7d2e5a9b310"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("api_keys", "suspended_at")
