"""A client's plan and credit, and the Sign server's way to spend it.

Two audiences:

  * **The client**, from the dashboard or with an API key, reads its own plan,
    what it has left and what it has used. Nothing here lets a client change
    its plan or its credit: plans are changed by Pangolin staff
    (console_billing.py), and credit will come from paid top-ups.
  * **The Pangolin Sign server** reserves a unit when a document is sent for
    signature, charges it when the request goes out, and hands it back when
    the request is cancelled before that. It presents SIGN_BILLING_SECRET, never
    a client's credentials: a client that could call these could refund itself.
"""

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import current_client
from app.core import security
from app.core.config import settings
from app.core.plans import PLAN_FEATURES
from app.db.models import Account, UsageEvent, Verification
from app.db.session import get_session
from app.schemas.billing import (
    BillingResponse,
    SignUsageRequest,
    SignUsageResponse,
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
    product: str | None = Query(default=None, pattern="^(kyc|sign)$"),
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


def sign_server(x_billing_secret: str | None = Header(default=None, alias="X-Billing-Secret")) -> None:
    """Only the Sign server, holding the shared secret. Not configured reads as not
    there at all, as legacy_only does."""
    if not settings.SIGN_BILLING_SECRET:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if not x_billing_secret or not security.same_secret(x_billing_secret, settings.SIGN_BILLING_SECRET):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid billing secret")


def _sign_account(db: Session, account_id: int) -> Account:
    account = db.get(Account, account_id)
    if account is None or not account.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No active account with that id")
    return account


def _sign_response(event) -> SignUsageResponse:
    if event is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Nothing reserved for that request, or it is already settled the other way")
    return SignUsageResponse(kind=event.kind, grant_id=event.grant_id, unit_price_cents=event.unit_price_cents)


@router.post("/sign/reserve", response_model=SignUsageResponse, dependencies=[Depends(sign_server)])
def sign_reserve(body: SignUsageRequest, db: Session = Depends(get_session)) -> SignUsageResponse:
    """Hold one Sign unit for a request about to be sent. 402 when none is left."""
    account = _sign_account(db, body.account_id)
    if not billing.allows(account, "e_signature"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This plan does not include e-signature")
    try:
        event = billing.reserve(db, account.id, "sign", external_ref=body.request_id)
    except billing.OutOfCredit as refused:
        db.rollback()
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, str(refused))
    db.commit()
    return _sign_response(event)


@router.post("/sign/commit", response_model=SignUsageResponse, dependencies=[Depends(sign_server)])
def sign_commit(body: SignUsageRequest, db: Session = Depends(get_session)) -> SignUsageResponse:
    """Charge the unit held for a request that has gone out to its signers."""
    account = _sign_account(db, body.account_id)
    event = billing.commit(db, account.id, "sign", external_ref=body.request_id)
    db.commit()
    return _sign_response(event)


@router.post("/sign/release", response_model=SignUsageResponse, dependencies=[Depends(sign_server)])
def sign_release(body: SignUsageRequest, db: Session = Depends(get_session)) -> SignUsageResponse:
    """Hand back the unit held for a request that was cancelled before it went out."""
    account = _sign_account(db, body.account_id)
    event = billing.release(db, account.id, "sign", external_ref=body.request_id)
    db.commit()
    return _sign_response(event)
