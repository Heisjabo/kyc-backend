"""A client's plan and credit, for Pangolin staff in the admin console.

Anyone with Pangolin KYC in their console products can read it. Changing a plan
or giving credit is for owners and admins, and both are kept in audit_events
(billing.plan_changed, billing.credit_granted), the way account changes are.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import console_product, console_role
from app.api.v1.routes.billing import billing_view
from app.api.v1.routes.console_team import _record
from app.db.models import Account, CreditGrant
from app.db.session import get_session
from app.schemas.billing import ConsoleBillingResponse, CreditGrantItem, CreditGrantRequest, PlanUpdate
from app.services import billing

router = APIRouter(prefix="/console/clients", tags=["admin console"])

_MANAGERS = ("owner", "admin")


def _client(db: Session, client_id: int) -> Account:
    account = db.get(Account, client_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No account with that id")
    return account


def _view(db: Session, account: Account) -> ConsoleBillingResponse:
    grants = db.scalars(
        select(CreditGrant).where(CreditGrant.account_id == account.id).order_by(CreditGrant.id.desc())
    ).all()
    return ConsoleBillingResponse(
        **billing_view(db, account),
        grants=[
            CreditGrantItem(
                id=g.id,
                product=g.product,
                source=g.source,
                quantity=g.quantity,
                remaining=g.remaining,
                unit_price_cents=g.unit_price_cents,
                note=g.note,
                created_at=g.created_at,
            )
            for g in grants
        ],
    )


@router.get("/{client_id}/billing", response_model=ConsoleBillingResponse)
def read_client_billing(
    client_id: int,
    _staff: Account = Depends(console_product("kyc")),
    db: Session = Depends(get_session),
) -> ConsoleBillingResponse:
    """The client's plan, balances and every grant it has had."""
    return _view(db, _client(db, client_id))


@router.put("/{client_id}/plan", response_model=ConsoleBillingResponse)
def update_client_plan(
    client_id: int,
    body: PlanUpdate,
    request: Request,
    staff: Account = Depends(console_role("kyc", _MANAGERS)),
    db: Session = Depends(get_session),
) -> ConsoleBillingResponse:
    """Move a client to another plan. An agreed rate and overdraft are kept only on
    Enterprise; moving off it clears them."""
    account = _client(db, client_id)
    before = {"plan": account.plan, "unit_price_cents": account.unit_price_cents, "allow_overdraft": account.allow_overdraft}
    billing.set_plan(
        db, account, body.plan, unit_price_cents=body.unit_price_cents, allow_overdraft=body.allow_overdraft
    )
    after = {"plan": account.plan, "unit_price_cents": account.unit_price_cents, "allow_overdraft": account.allow_overdraft}
    _record(db, staff, "billing.plan_changed", request, account_id=account.id, before=before, after=after)
    db.commit()
    return _view(db, account)


@router.post("/{client_id}/credits", response_model=ConsoleBillingResponse, status_code=status.HTTP_201_CREATED)
def grant_client_credit(
    client_id: int,
    body: CreditGrantRequest,
    request: Request,
    staff: Account = Depends(console_role("kyc", _MANAGERS)),
    db: Session = Depends(get_session),
) -> ConsoleBillingResponse:
    """Give a client units by hand, with the reason: a goodwill credit, a trial
    extension, or a payment taken outside the card checkout."""
    account = _client(db, client_id)
    credit = billing.grant(
        db, account.id, body.product, body.quantity,
        source="manual", unit_price_cents=body.unit_price_cents, note=body.note, granted_by_id=staff.id,
    )
    _record(
        db, staff, "billing.credit_granted", request,
        account_id=account.id, grant_id=credit.id, product=body.product, quantity=body.quantity, note=body.note,
    )
    db.commit()
    return _view(db, account)
