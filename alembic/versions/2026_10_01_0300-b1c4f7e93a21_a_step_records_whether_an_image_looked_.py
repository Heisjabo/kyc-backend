"""a step records whether an image looked generated

Revision ID: b1c4f7e93a21
Revises: dc771820ad4a
Create Date: 2026-10-01

Every step can receive more than one image: a front and a back, a bill and its
second page, the frames of a face check. This is true when any of them scored at or
above AI_IMAGE_DETECT_THRESHOLD, false when they were scored and none did, and null
when the step could not look: no image large enough, or the detector not installed.

It never changes a step's status. The score and the per-image scores stay in
`result`, under the ai_generated check.
"""

import sqlalchemy as sa
from alembic import op

revision = "b1c4f7e93a21"
down_revision = "dc771820ad4a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("verification_steps", sa.Column("ai_generated", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("verification_steps", "ai_generated")
