"""What a client may use, what it has left, and what it has used.

Credit is counted in units: one verification. An account's
balance is the `remaining` of its credit grants (models.CreditGrant), and every
unit that moves is written down in usage_events, in three moves:

  * **reserve**, when the work is opened. A unit is taken off a grant there and
    then, so two requests racing for the last unit cannot both get it.
  * **commit**, when it is decided (passed, failed or sent to review). This is
    the charge. Nothing moves on the grant; the unit was already taken.
  * **release**, when it never was decided: the link expired. The unit goes
    back on the grant it came from.

Free grants are spent before paid ones, then the oldest first, so paid credit
is kept for last.

Nothing here commits the database session: each function is called inside the
caller's transaction, so a verification and the unit it reserved are written
together or not at all.
"""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.plans import FREE_GRANTS, PLAN_FEATURES, PLANS, SERVICE_FEATURES, STANDARD_UNIT_CENTS
from app.db.models import Account, CreditGrant, UsageEvent, Verification, VerificationStatus

# The statuses a verification is charged for. Expired is not one: the person
# never finished, so the client is not billed.
DECIDED = (VerificationStatus.passed, VerificationStatus.failed, VerificationStatus.needs_review)


class OutOfCredit(Exception):
    """The account has no units left for this product."""

    def __init__(self, product: str):
        self.product = product
        super().__init__(f"No {product} credit left on this account. Top up to continue.")


class NotInPlan(Exception):
    """The account's plan does not include a feature it asked for."""

    def __init__(self, features: list[str], plan: str):
        self.features = features
        self.plan = plan
        super().__init__(f"The {plan} plan does not include: {', '.join(features)}. Upgrade to use it.")


# What the account may use.


def allows(account: Account, feature: str) -> bool:
    """Whether the account's plan includes `feature`. Always true while billing is
    not enforced, so turning billing on is the only thing that can refuse."""
    if not settings.BILLING_ENFORCED:
        return True
    return feature in PLAN_FEATURES.get(account.plan, PLAN_FEATURES["free"])


def check_services(account: Account, services: list[str]) -> None:
    """Refuse a verification that asks for a service outside the account's plan."""
    missing = [
        SERVICE_FEATURES[name]
        for name in services
        if name in SERVICE_FEATURES and not allows(account, SERVICE_FEATURES[name])
    ]
    if missing:
        raise NotInPlan(missing, account.plan)


def unit_price(account: Account) -> int:
    """What one unit costs this account when it is not covered by a grant, in cents."""
    if account.plan == "free":
        return 0
    return account.unit_price_cents


# Grants.


def _lock(db: Session, account_id: int) -> Account:
    """The account row, locked until the caller's transaction ends. Every change to
    an account's credit goes through here first, so they happen one at a time."""
    return db.scalar(select(Account).where(Account.id == account_id).with_for_update())


def ensure_free_grants(db: Session, account_id: int) -> None:
    """Give the account its free allowance, if it has never had it.

    Done when the account first touches billing rather than at sign-up, so it
    reaches every account however it was made: signed up, made by staff in the
    console, or made before billing existed. The partial unique index on
    credit_grants is what makes it once only.
    """
    _lock(db, account_id)
    given = set(
        db.scalars(
            select(CreditGrant.product).where(
                CreditGrant.account_id == account_id, CreditGrant.source == "free_grant"
            )
        )
    )
    for product, quantity in FREE_GRANTS.items():
        if product not in given:
            db.add(
                CreditGrant(
                    account_id=account_id,
                    product=product,
                    source="free_grant",
                    quantity=quantity,
                    remaining=quantity,
                    unit_price_cents=0,
                )
            )
    db.flush()


def grant(
    db: Session,
    account_id: int,
    product: str,
    quantity: int,
    *,
    source: str = "manual",
    unit_price_cents: int = 0,
    note: str | None = None,
    granted_by_id: int | None = None,
    top_up_id: int | None = None,
) -> CreditGrant:
    """Add credit to an account: staff giving units by hand now, a paid top-up later."""
    _lock(db, account_id)
    credit = CreditGrant(
        account_id=account_id,
        product=product,
        source=source,
        quantity=quantity,
        remaining=quantity,
        unit_price_cents=unit_price_cents,
        note=note,
        granted_by_id=granted_by_id,
        top_up_id=top_up_id,
    )
    db.add(credit)
    db.flush()
    return credit


def set_plan(
    db: Session,
    account: Account,
    plan: str,
    *,
    unit_price_cents: int | None = None,
    allow_overdraft: bool = False,
) -> Account:
    if plan not in PLANS:
        raise ValueError(f"plan must be one of {', '.join(PLANS)}")
    if plan != account.plan:
        account.plan = plan
        account.plan_changed_at = datetime.now(UTC)
    # A negotiated rate and running past zero are Enterprise terms only.
    agreed = plan == "enterprise" and unit_price_cents is not None
    account.unit_price_cents = unit_price_cents if agreed else STANDARD_UNIT_CENTS
    account.allow_overdraft = allow_overdraft if plan == "enterprise" else False
    db.flush()
    return account


# Spending.


def _subject(product: str, verification_id: int | None, external_ref: str | None):
    if verification_id is not None:
        return UsageEvent.verification_id == verification_id
    return (UsageEvent.product == product) & (UsageEvent.external_ref == external_ref)


def _event(db: Session, account_id: int, product: str, kind: str, verification_id, external_ref) -> UsageEvent | None:
    return db.scalar(
        select(UsageEvent).where(
            UsageEvent.account_id == account_id,
            UsageEvent.kind == kind,
            _subject(product, verification_id, external_ref),
        )
    )


def reserve(
    db: Session,
    account_id: int,
    product: str,
    *,
    verification_id: int | None = None,
    external_ref: str | None = None,
) -> UsageEvent:
    """Take one unit for a verification that is starting.

    Raises OutOfCredit when nothing is left and the account may not run past
    zero. A second reserve for the same subject returns the first.
    """
    account = _lock(db, account_id)
    existing = _event(db, account_id, product, "reserve", verification_id, external_ref)
    if existing is not None:
        return existing
    ensure_free_grants(db, account_id)

    source = db.scalar(
        select(CreditGrant)
        .where(CreditGrant.account_id == account_id, CreditGrant.product == product, CreditGrant.remaining > 0)
        # Free credit first (price 0), then the oldest paid grant.
        .order_by(CreditGrant.unit_price_cents.asc(), CreditGrant.id.asc())
        .limit(1)
        .with_for_update()
    )
    if source is None and settings.BILLING_ENFORCED and not account.allow_overdraft:
        raise OutOfCredit(product)

    if source is not None:
        source.remaining -= 1
    event = UsageEvent(
        account_id=account_id,
        product=product,
        kind="reserve",
        verification_id=verification_id,
        external_ref=external_ref,
        grant_id=source.id if source else None,
        unit_price_cents=source.unit_price_cents if source else unit_price(account),
    )
    db.add(event)
    db.flush()
    return event


def commit(
    db: Session,
    account_id: int,
    product: str,
    *,
    verification_id: int | None = None,
    external_ref: str | None = None,
) -> UsageEvent | None:
    """Charge the unit reserved for this subject. Does nothing when there was no
    reservation (work opened before billing existed), when it is already charged,
    or when it was released."""
    reserved = _event(db, account_id, product, "reserve", verification_id, external_ref)
    if reserved is None:
        return None
    for kind in ("commit", "release"):
        done = _event(db, account_id, product, kind, verification_id, external_ref)
        if done is not None:
            return done if kind == "commit" else None
    event = UsageEvent(
        account_id=account_id,
        product=product,
        kind="commit",
        verification_id=verification_id,
        external_ref=external_ref,
        grant_id=reserved.grant_id,
        unit_price_cents=reserved.unit_price_cents,
    )
    db.add(event)
    db.flush()
    return event


def release(
    db: Session,
    account_id: int,
    product: str,
    *,
    verification_id: int | None = None,
    external_ref: str | None = None,
) -> UsageEvent | None:
    """Hand back the unit reserved for this subject. Never after it was charged:
    a decided verification that later sits open and expires is still billed."""
    reserved = _event(db, account_id, product, "reserve", verification_id, external_ref)
    if reserved is None:
        return None
    if _event(db, account_id, product, "commit", verification_id, external_ref) is not None:
        return None
    done = _event(db, account_id, product, "release", verification_id, external_ref)
    if done is not None:
        return done

    _lock(db, account_id)
    if reserved.grant_id is not None:
        source = db.get(CreditGrant, reserved.grant_id, with_for_update=True)
        if source is not None:
            source.remaining += 1
    event = UsageEvent(
        account_id=account_id,
        product=product,
        kind="release",
        verification_id=verification_id,
        external_ref=external_ref,
        grant_id=reserved.grant_id,
        unit_price_cents=reserved.unit_price_cents,
    )
    db.add(event)
    db.flush()
    return event


def on_status(db: Session, verification: Verification) -> None:
    """Charge or hand back a verification's unit, from where it now stands. Called
    wherever a verification's status changes, before that change is committed."""
    if verification.status in DECIDED:
        commit(db, verification.account_id, "kyc", verification_id=verification.id)
    elif verification.status == VerificationStatus.expired:
        release(db, verification.account_id, "kyc", verification_id=verification.id)


# Reading.


def balances(db: Session, account_id: int) -> dict[str, dict]:
    """Per product: units left, units held by work still open, and units charged
    this calendar month (UTC)."""
    month_start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    remaining = dict(
        db.execute(
            select(CreditGrant.product, func.coalesce(func.sum(CreditGrant.remaining), 0))
            .where(CreditGrant.account_id == account_id)
            .group_by(CreditGrant.product)
        ).all()
    )
    counts = {
        (product, kind): count
        for product, kind, count in db.execute(
            select(UsageEvent.product, UsageEvent.kind, func.count())
            .where(UsageEvent.account_id == account_id)
            .group_by(UsageEvent.product, UsageEvent.kind)
        ).all()
    }
    this_month = dict(
        db.execute(
            select(UsageEvent.product, func.count())
            .where(
                UsageEvent.account_id == account_id,
                UsageEvent.kind == "commit",
                UsageEvent.created_at >= month_start,
            )
            .group_by(UsageEvent.product)
        ).all()
    )
    out = {}
    for product in FREE_GRANTS:
        reserved = counts.get((product, "reserve"), 0)
        settled = counts.get((product, "commit"), 0) + counts.get((product, "release"), 0)
        out[product] = {
            "remaining": int(remaining.get(product, 0)),
            "in_progress": reserved - settled,
            "used_total": counts.get((product, "commit"), 0),
            "used_this_month": this_month.get(product, 0),
        }
    return out
