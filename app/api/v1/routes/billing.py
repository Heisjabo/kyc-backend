"""A client's plan and credit.

The client, from the dashboard or with an API key, reads its own plan, what it
has left and what it has used. Nothing here lets a client change its plan or its
credit: plans are changed by staff (console_billing.py), and credit will come
from paid top-ups.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import current_client
from app.core.config import settings
from app.core.plans import PLAN_FEATURES
from app.db.models import Account, UsageEvent, Verification
from app.db.session import get_session
from app.schemas.billing import (
    BillingResponse,
    UsageItem,
    UsageList,
)
from app.services import billing

router = APIRouter(prefix="/billing", tags=["billing"])


def billing_view(db: Session, account: Account) -> dict:
    """The plan and balances, as the client and the console both show them."""
    billing.ensure_free_grants(db, account.id)
    db.commit()
    return {
        "plan": account.plan,
        "plan_changed_at": account.plan_changed_at,
        "unit_price_cents": account.unit_price_cents,
        "allow_overdraft": account.allow_overdraft,
        "enforced": settings.BILLING_ENFORCED,
        "features": sorted(PLAN_FEATURES.get(account.plan, PLAN_FEATURES["free"])),
        "balances": billing.balances(db, account.id),
    }


@router.get("", response_model=BillingResponse)
def read_billing(account: Account = Depends(current_client), db: Session = Depends(get_session)) -> BillingResponse:
    """The account's plan, the features it includes, and per product what is left,
    what is in progress and what has been used."""
    return BillingResponse(**billing_view(db, account))


@router.get("/usage", response_model=UsageList)
def list_usage(
    product: str | None = Query(default=None, pattern="^kyc$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    account: Account = Depends(current_client),
    db: Session = Depends(get_session),
) -> UsageList:
    """Every unit reserved, charged or handed back, newest first."""
    where = [UsageEvent.account_id == account.id]
    if product:
        where.append(UsageEvent.product == product)
    total = db.scalar(select(func.count()).select_from(UsageEvent).where(*where))
    rows = db.execute(
        select(UsageEvent, Verification.reference)
        .outerjoin(Verification, Verification.id == UsageEvent.verification_id)
        .where(*where)
        .order_by(UsageEvent.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return UsageList(
        items=[
            UsageItem(
                product=event.product,
                kind=event.kind,
                subject=reference or event.external_ref,
                unit_price_cents=event.unit_price_cents,
                created_at=event.created_at,
            )
            for event, reference in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )

