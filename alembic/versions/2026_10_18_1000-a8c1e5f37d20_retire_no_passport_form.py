"""retire the no passport net worth form

Revision ID: a8c1e5f37d20
Revises: f2b7d4e9a613
Create Date: 2026-10-18

Archives `net_worth_assessment_no_passport`. The person now says whether they
hold a passport on the document step, by ticking a section such as "I don't
have a passport but I am a citizen of my registered country", so the typed
"confirm" on the form asks the same thing twice.

Archived, not deleted: answers already given to it keep their form, and a client
sending this form_id is refused with 422 like any other inactive form. Clients
send `net_worth_assessment` with `document_groups` instead.

Data only, no schema change.
"""

import sqlalchemy as sa
from alembic import op

revision = "a8c1e5f37d20"
down_revision = "f2b7d4e9a613"
branch_labels = None
depends_on = None

PUBLIC_ID = "net_worth_assessment_no_passport"


def upgrade() -> None:
    op.execute(
        sa.text("UPDATE due_diligence_forms SET status = 'archived' WHERE public_id = :public_id").bindparams(
            public_id=PUBLIC_ID
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text("UPDATE due_diligence_forms SET status = 'active' WHERE public_id = :public_id").bindparams(
            public_id=PUBLIC_ID
        )
    )
