"""due diligence forms

Revision ID: b5e2c8d1f4a7
Revises: ad7d0a16fed7
Create Date: 2026-10-05

The forms behind the due diligence step, written in the admin console:

  due_diligence_forms          the form, its questions (JSONB) and its status
  due_diligence_form_clients   which clients may attach it to a verification

file_kind gains due_diligence_document, for files attached to an answer.

The Net worth assessment the Shufti journey asks today is added as an active
form, with no clients: console staff choose who gets it.

Additive: new tables, one new enum value and one row.
"""

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b5e2c8d1f4a7"
down_revision = "ad7d0a16fed7"
branch_labels = None
depends_on = None

NET_WORTH_FIELDS = [
    {
        "key": "funding_sources",
        "label": "Kindly confirm how you are funding your investment",
        "type": "multi_select",
        "required": True,
        "options": [
            "Employment/self employment income",
            "Personal savings",
            "Business income",
            "Investments or dividends",
            "Sale of property or assets",
            "Inheritance or gift",
            "Pension",
            "Other",
        ],
    },
    {
        "key": "funding_country",
        "label": "Kindly confirm from which country you are funding your investment",
        "type": "country",
        "required": True,
    },
    {
        "key": "supporting_document",
        "label": (
            "Please upload a supporting document with respect to the above (e.g bank statement, "
            "portfolio investment, confirmation letter or any other document showing net worth)"
        ),
        "help": (
            "You may upload a bank statement, portfolio investment, confirmation letter or any "
            "other document showing net worth."
        ),
        "type": "file",
        "required": True,
    },
    {
        "key": "amount_usd",
        "label": "Kindly advise on the amount you intend to invest (in USD).",
        "type": "amount",
        "required": True,
    },
]


def upgrade() -> None:
    op.execute("ALTER TYPE file_kind ADD VALUE IF NOT EXISTS 'due_diligence_document'")

    op.create_table(
        "due_diligence_forms",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("public_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.String(1000), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("fields", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_by_id", sa.BigInteger(), sa.ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_due_diligence_forms_public_id", "due_diligence_forms", ["public_id"], unique=True)

    op.create_table(
        "due_diligence_form_clients",
        sa.Column("form_id", sa.BigInteger(), sa.ForeignKey("due_diligence_forms.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_due_diligence_form_clients_account_id", "due_diligence_form_clients", ["account_id"])

    op.execute(
        sa.text(
            "INSERT INTO due_diligence_forms (public_id, name, description, status, fields) "
            "VALUES (:public_id, :name, :description, 'active', CAST(:fields AS jsonb)) "
            "ON CONFLICT (public_id) DO NOTHING"
        ).bindparams(
            public_id="net_worth_assessment",
            name="Net worth assessment",
            # None, as on Shufti: the form opens straight on its first question.
            description=None,
            fields=json.dumps(NET_WORTH_FIELDS),
        )
    )


def downgrade() -> None:
    op.drop_index("ix_due_diligence_form_clients_account_id", table_name="due_diligence_form_clients")
    op.drop_table("due_diligence_form_clients")
    op.drop_index("ix_due_diligence_forms_public_id", table_name="due_diligence_forms")
    op.drop_table("due_diligence_forms")
    # A value cannot be dropped from a Postgres enum; due_diligence_document stays.
