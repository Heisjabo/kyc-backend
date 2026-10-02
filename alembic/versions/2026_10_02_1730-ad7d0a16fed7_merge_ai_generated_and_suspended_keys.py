"""merge the ai_generated and suspended_at migrations

Revision ID: ad7d0a16fed7
Revises: d4a91c6e2f83, e8b3f1a4c702
Create Date: 2026-10-02

Two pull requests each added a migration on top of c7d2e5a9b310 and were merged
one after the other:

  d4a91c6e2f83  verification_steps.ai_generated   (AI-generated image detection)
  e8b3f1a4c702  api_keys.suspended_at             (suspending API keys)

Each was fine alone. Together they left the history with two heads, and
`alembic upgrade head` refuses to choose between them, so the container stopped
at start-up on "Multiple head revisions are present" and restarted in a loop.

This revision changes no schema. It only joins the two, which leaves one head.
The order of the two parents does not matter: they touch different tables. A
database at c7d2e5a9b310 gets both; one that already has either gets the other.
"""

revision = "ad7d0a16fed7"
down_revision = ("d4a91c6e2f83", "e8b3f1a4c702")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
