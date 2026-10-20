"""What the billing endpoints take and give back (app/api/v1/routes/billing.py,
app/api/v1/routes/console_billing.py)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Plan = Literal["free", "payg", "enterprise"]
Product = Literal["kyc"]


class ProductBalance(BaseModel):
    # Units the account can still spend.
    remaining: int
    # Units held by verifications that are still open.
    in_progress: int
    used_total: int
    used_this_month: int


class BillingResponse(BaseModel):
    plan: Plan
    plan_changed_at: datetime | None
    # Cents per unit once free credit is spent: 100 ($1), or the account's
    # Enterprise rate.
    unit_price_cents: int
    allow_overdraft: bool
    enforced: bool
    features: list[str]
    balances: dict[str, ProductBalance]


class UsageItem(BaseModel):
    product: str
    kind: str
    # The verification's reference.
    subject: str | None
    unit_price_cents: int
    created_at: datetime


class UsageList(BaseModel):
    items: list[UsageItem]
    total: int
    page: int
    page_size: int


class PlanUpdate(BaseModel):
    plan: Plan
    # Enterprise only: an agreed rate in cents (left out, it stays $1), and
    # whether usage may run past zero.
    unit_price_cents: int | None = Field(default=None, ge=0)
    allow_overdraft: bool = False


class CreditGrantRequest(BaseModel):
    product: Product
    quantity: int = Field(ge=1, le=1_000_000)
    # What each unit was paid for, in cents: 0 for a goodwill credit, the price
    # paid for a payment taken outside the card checkout.
    unit_price_cents: int = Field(default=0, ge=0)
    note: str = Field(min_length=1, max_length=500)


class CreditGrantItem(BaseModel):
    id: int
    product: str
    source: str
    quantity: int
    remaining: int
    unit_price_cents: int
    note: str | None
    created_at: datetime


class ConsoleBillingResponse(BillingResponse):
    grants: list[CreditGrantItem]
