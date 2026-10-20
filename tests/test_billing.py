"""Plans, credit and usage (app/services/billing.py).

Run against Postgres, like the session tests, because what is under test is
what gets written down and the row locks that keep two requests from spending
the same unit.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.models import Account, CreditGrant, StepStatus, UsageEvent, Verification, VerificationStatus
from app.services import billing
from app.services import verifications as service
from tests.test_console_verifications import (  # noqa: F401 (fixtures)
    _reachable,
    auth,
    client,
    client_key,
    db_session,
    open_verification,
    staff_token,
)

pytestmark = pytest.mark.skipif(not _reachable(), reason="needs the local Postgres")

SIGN_SECRET = "sign-server-secret-for-tests"


@pytest.fixture()
def key(client):
    header, _ = client_key(client, "billing@example.com", "Billing")
    return header


@pytest.fixture()
def enforced(monkeypatch):
    monkeypatch.setattr(settings, "BILLING_ENFORCED", True)


def account_of(db_session, email="billing@example.com") -> Account:
    return db_session.scalar(select(Account).where(Account.email == email))


def open_one(client, key, **body):
    return client.post(
        "/v1/verifications",
        headers=key,
        json={"services": ["document"], "applicant": {"first_name": "Jean", "last_name": "Mugisha"}, **body},
    )


def kinds(db_session, verification_id) -> list[str]:
    return list(
        db_session.scalars(
            select(UsageEvent.kind).where(UsageEvent.verification_id == verification_id).order_by(UsageEvent.id)
        )
    )


def decide(db_session, reference, account, status=StepStatus.passed) -> Verification:
    verification = service.by_reference(db_session, account.id, reference)
    step = service.open_step(db_session, verification, verification.steps[0].step)
    service.finish_step(db_session, step, status=status)
    return service.settle(db_session, verification)


# The free allowance


def test_a_new_account_starts_free_with_100_kyc_and_100_sign(client, key):
    body = client.get("/v1/billing", headers=key).json()
    assert body["plan"] == "free"
    assert body["balances"]["kyc"]["remaining"] == 100
    assert body["balances"]["sign"]["remaining"] == 100
    assert body["unit_price_cents"] == 100
    assert "aml" not in body["features"] and "e_signature" in body["features"]


def test_the_free_allowance_is_given_once(client, key, db_session):
    client.get("/v1/billing", headers=key)
    client.get("/v1/billing", headers=key)
    account = account_of(db_session)
    billing.ensure_free_grants(db_session, account.id)
    grants = db_session.scalars(select(CreditGrant).where(CreditGrant.account_id == account.id)).all()
    assert sorted(g.product for g in grants) == ["kyc", "sign"]


# Reserve, commit, release


def test_opening_a_verification_takes_a_unit(client, key, db_session):
    created = open_one(client, key).json()
    verification = service.by_reference(db_session, account_of(db_session).id, created["reference"])
    assert kinds(db_session, verification.id) == ["reserve"]
    kyc = client.get("/v1/billing", headers=key).json()["balances"]["kyc"]
    assert kyc == {"remaining": 99, "in_progress": 1, "used_total": 0, "used_this_month": 0}


def test_a_decided_verification_is_charged_once(client, key, db_session):
    created = open_one(client, key).json()
    account = account_of(db_session)
    verification = decide(db_session, created["reference"], account)
    assert verification.status == VerificationStatus.passed
    service.settle(db_session, verification)  # settling again charges nothing more
    assert kinds(db_session, verification.id) == ["reserve", "commit"]
    kyc = client.get("/v1/billing", headers=key).json()["balances"]["kyc"]
    assert kyc == {"remaining": 99, "in_progress": 0, "used_total": 1, "used_this_month": 1}


@pytest.mark.parametrize("status", [StepStatus.failed, StepStatus.review])
def test_failed_and_review_are_charged_too(client, key, db_session, status):
    created = open_one(client, key).json()
    verification = decide(db_session, created["reference"], account_of(db_session), status)
    assert kinds(db_session, verification.id) == ["reserve", "commit"]


def test_an_expired_verification_gives_its_unit_back(client, key, db_session):
    created = open_one(client, key).json()
    account = account_of(db_session)
    verification = service.by_reference(db_session, account.id, created["reference"])
    verification.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db_session.commit()
    service.expire_if_due(db_session, verification)
    assert kinds(db_session, verification.id) == ["reserve", "release"]
    assert client.get("/v1/billing", headers=key).json()["balances"]["kyc"]["remaining"] == 100


def test_the_sweeper_gives_units_back_too(client, key, db_session):
    created = open_one(client, key).json()
    verification = service.by_reference(db_session, account_of(db_session).id, created["reference"])
    verification.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db_session.commit()
    service.expire_overdue(db_session)
    assert kinds(db_session, verification.id) == ["reserve", "release"]


def test_a_charged_verification_is_never_refunded(client, key, db_session):
    """Reopened by a reviewer's redo and then left to expire: still billed."""
    created = open_one(client, key).json()
    verification = decide(db_session, created["reference"], account_of(db_session))
    verification.status = VerificationStatus.in_progress
    verification.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db_session.commit()
    service.expire_if_due(db_session, verification)
    assert kinds(db_session, verification.id) == ["reserve", "commit"]


def test_verifications_from_before_billing_are_left_alone(client, key, db_session):
    created = open_one(client, key).json()
    account = account_of(db_session)
    verification = service.by_reference(db_session, account.id, created["reference"])
    db_session.query(UsageEvent).filter(UsageEvent.verification_id == verification.id).delete()
    db_session.commit()
    decide(db_session, created["reference"], account)
    assert kinds(db_session, verification.id) == []


def test_free_credit_is_spent_before_paid(client, key, db_session):
    account = account_of(db_session)
    billing.ensure_free_grants(db_session, account.id)
    paid = billing.grant(db_session, account.id, "kyc", 200, source="top_up", unit_price_cents=100)
    db_session.commit()
    open_one(client, key)
    db_session.refresh(paid)
    assert paid.remaining == 200


# When credit runs out


def test_out_of_credit_is_refused_when_enforced(client, key, db_session, enforced):
    account = account_of(db_session)
    billing.ensure_free_grants(db_session, account.id)
    free = db_session.scalar(select(CreditGrant).where(CreditGrant.account_id == account.id, CreditGrant.product == "kyc"))
    free.remaining = 0
    db_session.commit()
    response = open_one(client, key, reference="NO-CREDIT-1")
    assert response.status_code == 402
    assert db_session.scalar(select(Verification).where(Verification.reference == "NO-CREDIT-1")) is None


def test_out_of_credit_is_only_metered_while_not_enforced(client, key, db_session):
    account = account_of(db_session)
    billing.ensure_free_grants(db_session, account.id)
    for grant in db_session.scalars(select(CreditGrant).where(CreditGrant.account_id == account.id)):
        grant.remaining = 0
    db_session.commit()
    created = open_one(client, key)
    assert created.status_code == 201
    event = db_session.scalar(select(UsageEvent).where(UsageEvent.account_id == account.id))
    assert event.kind == "reserve" and event.grant_id is None


def test_enterprise_overdraft_runs_past_zero_at_its_rate(client, key, db_session, enforced):
    account = account_of(db_session)
    billing.ensure_free_grants(db_session, account.id)
    billing.set_plan(db_session, account, "enterprise", unit_price_cents=70, allow_overdraft=True)
    for grant in db_session.scalars(select(CreditGrant).where(CreditGrant.account_id == account.id)):
        grant.remaining = 0
    db_session.commit()
    assert open_one(client, key).status_code == 201
    event = db_session.scalar(select(UsageEvent).where(UsageEvent.account_id == account.id))
    assert event.grant_id is None and event.unit_price_cents == 70


# What a plan includes


def test_free_plan_leaves_aml_out_when_enforced(client, key, enforced):
    body = open_one(client, key, services=["document", "face"]).json()
    assert [step["step"] for step in body["steps"]] == ["document", "face"]


def test_free_plan_refuses_aml_asked_for_outright(client, key, enforced):
    response = open_one(client, key, services=["document", "face", "aml"])
    assert response.status_code == 403 and "aml" in response.json()["detail"]


def test_free_plan_refuses_address_verification(client, key, enforced):
    response = open_one(client, key, services=["address"], applicant={"address": "KN 5 Rd, Kigali"})
    assert response.status_code == 403


def test_paid_plan_gets_aml_added(client, key, db_session, enforced):
    billing.set_plan(db_session, account_of(db_session), "payg")
    db_session.commit()
    body = open_one(client, key, services=["document", "face"]).json()
    assert [step["step"] for step in body["steps"]] == ["document", "face", "aml"]


def test_nothing_is_refused_by_plan_while_not_enforced(client, key):
    body = open_one(client, key, services=["document", "face"]).json()
    assert [step["step"] for step in body["steps"]] == ["document", "face", "aml"]


# Sign, from the Sign server


@pytest.fixture()
def sign_secret(monkeypatch):
    monkeypatch.setattr(settings, "SIGN_BILLING_SECRET", SIGN_SECRET)
    return {"X-Billing-Secret": SIGN_SECRET}


def test_sign_endpoints_are_off_without_a_secret(client, key, db_session):
    account = account_of(db_session)
    response = client.post("/v1/billing/sign/reserve", json={"account_id": account.id, "request_id": "doc-1"})
    assert response.status_code == 404


def test_sign_endpoints_refuse_a_wrong_secret(client, key, db_session, sign_secret):
    account = account_of(db_session)
    response = client.post(
        "/v1/billing/sign/reserve",
        json={"account_id": account.id, "request_id": "doc-1"},
        headers={"X-Billing-Secret": "wrong"},
    )
    assert response.status_code == 401


def test_a_client_key_cannot_spend_or_refund_sign_units(client, key, db_session, sign_secret):
    account = account_of(db_session)
    response = client.post("/v1/billing/sign/release", json={"account_id": account.id, "request_id": "doc-1"}, headers=key)
    assert response.status_code == 401


def test_a_sign_request_is_reserved_then_charged(client, key, db_session, sign_secret):
    account = account_of(db_session)
    body = {"account_id": account.id, "request_id": "doc-1"}
    assert client.post("/v1/billing/sign/reserve", json=body, headers=sign_secret).json()["kind"] == "reserve"
    assert client.post("/v1/billing/sign/reserve", json=body, headers=sign_secret).status_code == 200
    assert client.post("/v1/billing/sign/commit", json=body, headers=sign_secret).json()["kind"] == "commit"
    assert client.post("/v1/billing/sign/release", json=body, headers=sign_secret).status_code == 409
    sign = client.get("/v1/billing", headers=key).json()["balances"]["sign"]
    assert sign == {"remaining": 99, "in_progress": 0, "used_total": 1, "used_this_month": 1}


def test_a_cancelled_sign_request_gives_its_unit_back(client, key, db_session, sign_secret):
    account = account_of(db_session)
    body = {"account_id": account.id, "request_id": "doc-2"}
    client.post("/v1/billing/sign/reserve", json=body, headers=sign_secret)
    assert client.post("/v1/billing/sign/release", json=body, headers=sign_secret).json()["kind"] == "release"
    assert client.post("/v1/billing/sign/commit", json=body, headers=sign_secret).status_code == 409
    assert client.get("/v1/billing", headers=key).json()["balances"]["sign"]["remaining"] == 100


# Usage history


def test_usage_lists_every_movement_newest_first(client, key, db_session):
    created = open_one(client, key).json()
    decide(db_session, created["reference"], account_of(db_session))
    usage = client.get("/v1/billing/usage", headers=key).json()
    assert [item["kind"] for item in usage["items"]] == ["commit", "reserve"]
    assert all(item["subject"] == created["reference"] for item in usage["items"])


# The console


def manager_token(client, db_session) -> str:
    token = staff_token(client, db_session)
    staff = account_of(db_session, "staff@example.com")
    staff.console_role = "admin"
    db_session.commit()
    return token


def test_staff_move_a_client_to_enterprise_and_back(client, key, db_session):
    token = manager_token(client, db_session)
    account = account_of(db_session)
    url = f"/v1/console/clients/{account.id}/plan"
    body = client.put(url, json={"plan": "enterprise", "unit_price_cents": 70, "allow_overdraft": True}, headers=auth(token)).json()
    assert body["plan"] == "enterprise" and body["unit_price_cents"] == 70 and body["allow_overdraft"] is True
    body = client.put(url, json={"plan": "payg", "unit_price_cents": 70, "allow_overdraft": True}, headers=auth(token)).json()
    assert body["plan"] == "payg" and body["unit_price_cents"] == 100 and body["allow_overdraft"] is False


def test_staff_give_credit_with_a_reason(client, key, db_session):
    token = manager_token(client, db_session)
    account = account_of(db_session)
    response = client.post(
        f"/v1/console/clients/{account.id}/credits",
        json={"product": "kyc", "quantity": 200, "unit_price_cents": 100, "note": "Paid by bank transfer"},
        headers=auth(token),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["balances"]["kyc"]["remaining"] == 300
    assert body["grants"][0]["source"] == "manual" and body["grants"][0]["note"] == "Paid by bank transfer"


def test_a_reviewer_cannot_change_a_plan(client, key, db_session):
    token = staff_token(client, db_session)
    account = account_of(db_session)
    response = client.put(f"/v1/console/clients/{account.id}/plan", json={"plan": "payg"}, headers=auth(token))
    assert response.status_code == 403
    assert client.get(f"/v1/console/clients/{account.id}/billing", headers=auth(token)).status_code == 200
