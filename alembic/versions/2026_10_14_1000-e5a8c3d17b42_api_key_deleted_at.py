"""api key deleted_at

Revision ID: e5a8c3d17b42
Revises: d7e4b1c09a32
Create Date: 2026-10-14

api_keys.deleted_at is set when a client deletes a key. The key is revoked
first and the row is kept, so what was done with it stays on record; the
client's own list leaves it out.

Additive: one nullable column.
"""

import sqlalchemy as sa
from alembic import op

revision = "e5a8c3d17b42"
down_revision = "d7e4b1c09a32"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("deleted_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("api_keys", "deleted_at")
