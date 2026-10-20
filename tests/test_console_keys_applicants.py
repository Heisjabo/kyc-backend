"""The admin console's lists of API keys and applicants, across every client.

Against Postgres in a throwaway schema, like the other API tests, and skipped
when it is not reachable.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.models import Account, ApiKey, Applicant, Verification, VerificationStatus
from tests.test_console_verifications import (  # noqa: F401 (fixtures)
    _reachable,
    auth,
    client,
    client_key,
    db_session,
    open_verification,
    staff_token,
    without_products,
)

pytestmark = pytest.mark.skipif(not _reachable(), reason="needs the local Postgres")


@pytest.fixture()
def setup(client, db_session):
    """Acme with two applicants (Amina verified twice) and Beta with one."""
    acme, _ = client_key(client, "ops@acme.example.com", "Acme")
    beta, _ = client_key(client, "ops@beta.example.com", "Beta")
    amina = open_verification(client, acme, "Amina", "Wanjiru")
    open_verification(client, acme, "Kwame", "Mensah")
    again = open_verification(client, acme, "Amina", "Wanjiru")
    open_verification(client, beta, "Grace", "Uwase")

    # The client API opens a new applicant every time; tie the retry to the first,
    # as a returning applicant would be.
    first = db_session.scalar(select(Applicant).where(Applicant.customer_id == amina["customer_id"]))
    retry = db_session.scalar(select(Verification).where(Verification.reference == again["reference"]))
    orphan = retry.applicant
    retry.applicant_id = first.id
    db_session.flush()
    db_session.delete(orphan)
    db_session.commit()
    return {"acme": acme, "beta": beta, "amina": amina, "again": again}


def get(client, token, path, **params):
    response = client.get(path, params=params, headers=auth(token))
    assert response.status_code == 200, response.text
    return response.json()


# API keys

def test_staff_see_every_clients_keys_with_their_state(client, db_session, setup):
    token = staff_token(client, db_session)
    keys = db_session.scalars(select(ApiKey).order_by(ApiKey.id)).all()
    keys[0].expires_at = datetime.now(UTC) + timedelta(days=10)
    db_session.commit()

    body = get(client, token, "/v1/console/api-keys")
    assert body["total"] == 2
    newest = body["items"][0]
    assert newest["client"]["email"] == "ops@beta.example.com"
    assert newest["state"] == "active"
    assert "secret_hash" not in newest and "key" not in newest
    assert body["items"][1]["state"] == "expiring"
    assert body["state_counts"] == {"active": 1, "expiring": 1, "suspended": 0, "expired": 0, "revoked": 0}


def test_key_filters(client, db_session, setup):
    token = staff_token(client, db_session)
    keys = db_session.scalars(select(ApiKey).order_by(ApiKey.id)).all()
    keys[0].revoked_at = datetime.now(UTC)
    keys[1].expires_at = datetime.now(UTC) - timedelta(days=1)
    db_session.commit()
    acme = db_session.scalar(select(Account).where(Account.email == "ops@acme.example.com"))

    prefixes = lambda **p: [k["prefix"] for k in get(client, token, "/v1/console/api-keys", **p)["items"]]  # noqa: E731
    assert prefixes(state="revoked") == [keys[0].prefix]
    assert prefixes(state="expired") == [keys[1].prefix]
    assert prefixes(client_id=acme.id) == [keys[0].prefix]
    assert prefixes(q="beta") == [keys[1].prefix]
    assert prefixes(q=keys[0].prefix) == [keys[0].prefix]
    assert prefixes(q=f"kyc_{keys[0].prefix}_secretpart") == [keys[0].prefix]
    # Counts follow the other filters, not the state.
    assert get(client, token, "/v1/console/api-keys", q="beta")["state_counts"]["expired"] == 1
    assert client.get("/v1/console/api-keys", params={"state": "old"}, headers=auth(token)).status_code == 422


# Applicants

def test_staff_see_every_applicant_with_their_latest_verification(client, db_session, setup):
    token = staff_token(client, db_session)
    body = get(client, token, "/v1/console/applicants")

    assert body["total"] == 3
    # Most recent activity first: Grace, then Amina (verified again), then Kwame.
    assert [a["name"] for a in body["items"]] == ["Grace Uwase", "Amina Wanjiru", "Kwame Mensah"]
    amina = body["items"][1]
    assert amina["verifications"] == 2
    assert amina["customer_id"] == setup["amina"]["customer_id"]
    assert amina["latest_status"] == "pending"
    assert amina["client"]["name"] == "Acme Ltd"
    assert body["status_counts"]["pending"] == 3


def test_applicant_filters(client, db_session, setup):
    token = staff_token(client, db_session)
    latest = db_session.scalar(select(Verification).where(Verification.reference == setup["again"]["reference"]))
    latest.status = VerificationStatus.passed
    kwame = db_session.scalar(select(Applicant).where(Applicant.first_name == "Kwame"))
    kwame.created_at = datetime(2025, 5, 1, tzinfo=UTC)
    kwame.phone = "+250 788 111 222"
    db_session.commit()
    acme = db_session.scalar(select(Account).where(Account.email == "ops@acme.example.com"))

    names = lambda **p: [a["name"] for a in get(client, token, "/v1/console/applicants", **p)["items"]]  # noqa: E731
    assert names(status="passed") == ["Amina Wanjiru"]
    assert names(client_id=acme.id) == ["Amina Wanjiru", "Kwame Mensah"]
    assert names(q="grace") == ["Grace Uwase"]
    assert names(q=setup["amina"]["customer_id"]) == ["Amina Wanjiru"]
    assert names(q="788111222") == ["Kwame Mensah"]
    assert names(created_from="2025-01-01", created_to="2025-12-31") == ["Kwame Mensah"]
    counts = get(client, token, "/v1/console/applicants")["status_counts"]
    assert (counts["passed"], counts["pending"]) == (1, 2)
    assert client.get("/v1/console/applicants", params={"status": "nope"}, headers=auth(token)).status_code == 422


def test_applicant_detail_has_every_verification_newest_first(client, db_session, setup):
    token = staff_token(client, db_session)
    amina = next(a for a in get(client, token, "/v1/console/applicants")["items"] if a["name"] == "Amina Wanjiru")

    detail = get(client, token, f"/v1/console/applicants/{amina['id']}")
    assert detail["first_name"] == "Amina"
    assert [v["reference"] for v in detail["history"]] == [setup["again"]["reference"], setup["amina"]["reference"]]
    assert client.get("/v1/console/applicants/999999", headers=auth(token)).status_code == 404


def test_keys_and_applicants_need_a_console_token_with_kyc(client, db_session, setup):
    _, dashboard_token = client_key(client, "ops@gamma.example.com", "Gamma")
    no_kyc = staff_token(client, db_session, email="noproducts@example.com")
    without_products(db_session, "noproducts@example.com")
    for path in ("/v1/console/api-keys", "/v1/console/applicants", "/v1/console/applicants/1"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers=auth(dashboard_token)).status_code == 401
        assert client.get(path, headers=auth(no_kyc)).status_code == 403
