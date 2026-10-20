"""billing plans, credit and usage

Revision ID: b4d7e2a9c158
Revises: a8c1e5f37d20
Create Date: 2026-10-20

Accounts gain a plan (free, payg or enterprise), a price per unit ($1, or an
agreed Enterprise rate) and an overdraft switch. Every existing account starts on free; staff move clients
to their real plan in the console before BILLING_ENFORCED is turned on.

Three new tables:
  * top_ups: card payments for credit (the provider is wired up later).
  * credit_grants: batches of units an account may spend. The free allowance
    is not written here: it is given the first time an account touches billing
    (app/services/billing.py, ensure_free_grants).
  * usage_events: every unit reserved, charged or handed back.

Additive: new columns with defaults and new tables. Nothing existing changes.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b4d7e2a9c158"
down_revision = "a8c1e5f37d20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("plan", sa.String(length=16), nullable=False, server_default="free"))
    op.add_column("accounts", sa.Column("plan_changed_at", sa.DateTime(timezone=True)))
    op.add_column(
        "accounts", sa.Column("unit_price_cents", sa.Integer(), nullable=False, server_default=sa.text("100"))
    )
    op.add_column(
        "accounts", sa.Column("allow_overdraft", sa.Boolean(), nullable=False, server_default=sa.text("false"))
    )

    op.create_table(
        "top_ups",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("reference", sa.String(length=128), nullable=False, unique=True),
        sa.Column("checkout_id", sa.String(length=128), unique=True),
        sa.Column("charge_id", sa.String(length=128)),
        sa.Column("units", sa.Integer(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_top_ups_account_id", "top_ups", ["account_id"])
    op.create_index("ix_top_ups_status", "top_ups", ["status"])

    op.create_table(
        "credit_grants",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("remaining", sa.Integer(), nullable=False),
        sa.Column("unit_price_cents", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("top_up_id", sa.BigInteger(), sa.ForeignKey("top_ups.id", ondelete="RESTRICT"), unique=True),
        sa.Column("note", sa.String(length=500)),
        sa.Column("granted_by_id", sa.BigInteger(), sa.ForeignKey("accounts.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_credit_grants_account_id", "credit_grants", ["account_id"])
    op.create_index(
        "uq_credit_grants_free_once",
        "credit_grants",
        ["account_id", "product"],
        unique=True,
        postgresql_where=sa.text("source = 'free_grant'"),
    )

    op.create_table(
        "usage_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("verification_id", sa.BigInteger(), sa.ForeignKey("verifications.id", ondelete="SET NULL")),
        sa.Column("external_ref", sa.String(length=128)),
        sa.Column("grant_id", sa.BigInteger(), sa.ForeignKey("credit_grants.id", ondelete="SET NULL")),
        sa.Column("unit_price_cents", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("verification_id", "kind", name="uq_usage_events_verification_id_kind"),
        sa.UniqueConstraint(
            "account_id", "product", "external_ref", "kind", name="uq_usage_events_account_id_product_external_ref_kind"
        ),
    )
    op.create_index("ix_usage_events_account_id", "usage_events", ["account_id"])
    op.create_index("ix_usage_events_verification_id", "usage_events", ["verification_id"])
    op.create_index("ix_usage_events_grant_id", "usage_events", ["grant_id"])


def downgrade() -> None:
    op.drop_table("usage_events")
    op.drop_table("credit_grants")
    op.drop_table("top_ups")
    op.drop_column("accounts", "allow_overdraft")
    op.drop_column("accounts", "unit_price_cents")
    op.drop_column("accounts", "plan_changed_at")
    op.drop_column("accounts", "plan")
