"""no passport net worth form

Revision ID: f2b7d4e9a613
Revises: e5a8c3d17b42
Create Date: 2026-10-16

Adds `net_worth_assessment_no_passport`: the Net worth assessment as it stands
now, with Shufti's passport confirmation as its last question. The person types
"confirm" to say they do not hold a valid passport. A client sends this form_id
for people who are verifying with a national ID or driving licence, and the
plain form for passport holders.

The new form is given to every client the original form was given to.

Additive: one form row and its client rows. Questions live in the form's JSON,
so the new `expected` setting on a question needs no column.
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "f2b7d4e9a613"
down_revision = "e5a8c3d17b42"
branch_labels = None
depends_on = None

PUBLIC_ID = "net_worth_assessment_no_passport"

PASSPORT_CONFIRMATION = {
    "key": "no_passport_confirmation",
    "label": "I confirm that I do not hold a valid passport",
    "type": "text",
    "required": True,
    "help": 'Type "confirm" in the box.',
    "placeholder": "confirm",
    "options": [],
    "expected": "confirm",
}


def upgrade() -> None:
    op.execute(
        sa.text(
            "INSERT INTO due_diligence_forms (public_id, name, description, status, fields) "
            "SELECT :public_id, 'Net worth assessment + Passport confirmation', description, 'active', "
            "fields || CAST(:confirmation AS jsonb) "
            "FROM due_diligence_forms WHERE public_id = 'net_worth_assessment' "
            "ON CONFLICT (public_id) DO NOTHING"
        ).bindparams(public_id=PUBLIC_ID, confirmation=json.dumps([PASSPORT_CONFIRMATION]))
    )
    op.execute(
        sa.text(
            "INSERT INTO due_diligence_form_clients (form_id, account_id) "
            "SELECT copy.id, given.account_id "
            "FROM due_diligence_form_clients given "
            "JOIN due_diligence_forms original ON original.id = given.form_id "
            "AND original.public_id = 'net_worth_assessment' "
            "JOIN due_diligence_forms copy ON copy.public_id = :public_id "
            "ON CONFLICT DO NOTHING"
        ).bindparams(public_id=PUBLIC_ID)
    )


def downgrade() -> None:
    # Client rows go with it (ON DELETE CASCADE).
    op.execute(sa.text("DELETE FROM due_diligence_forms WHERE public_id = :public_id").bindparams(public_id=PUBLIC_ID))
