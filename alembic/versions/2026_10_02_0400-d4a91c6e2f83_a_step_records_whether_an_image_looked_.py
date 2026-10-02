"""a step records whether an image looked generated

Revision ID: d4a91c6e2f83
Revises: c7d2e5a9b310
Create Date: 2026-10-02

Every step can receive more than one image: a front and a back, a bill and its
second page, the frames of a face check. verification_steps.ai_generated is true
when any of them scored at or above AI_IMAGE_DETECT_THRESHOLD, false when they were
scored and none did, and null when the step could not look: no image large enough,
or the detector not installed. It never changes a step's status.

ADD COLUMN IF NOT EXISTS, deliberately. This column was added once before, in
b1c4f7e93a21, applied to the production and development databases, and then that
revision was deleted by the revert of the feature while the column stayed behind.
Both databases therefore already have it, and a plain ADD COLUMN would fail on them
while passing on a fresh one, which is the worst way for a migration to behave.
"""

from alembic import op

revision = "d4a91c6e2f83"
down_revision = "c7d2e5a9b310"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE verification_steps ADD COLUMN IF NOT EXISTS ai_generated boolean")


def downgrade() -> None:
    op.execute("ALTER TABLE verification_steps DROP COLUMN IF EXISTS ai_generated")
