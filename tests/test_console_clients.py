"""The admin console's view of client accounts: every one, filtered and paged.

Against Postgres in a throwaway schema, like the other API tests, and skipped
when it is not reachable.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.models import Account, ApiKey, Verification, VerificationStatus
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


@pytest.fixture()
def clients(client):
    """Acme with two verifications and Beta with none, made in that order."""
    acme, _ = client_key(client, "ops@acme.example.com", "Acme")
    client_key(client, "ops@beta.example.com", "Beta")
    open_verification(client, acme, "Amina", "Wanjiru")
    open_verification(client, acme, "Kwame", "Mensah")
    return acme


def listing(client, token, **params):
    response = client.get("/v1/console/clients", params=params, headers=auth(token))
    assert response.status_code == 200, response.text
    return response.json()


def test_staff_see_every_client_newest_first_with_counts(client, db_session, clients):
    token = staff_token(client, db_session)
    body = listing(client, token)

    # Staff accounts are listed with the clients (a0c2881): the staff account made here
    # signed in last, so it is first, with a role and no verifications.
    assert [c["email"] for c in body["items"]] == [
        "staff@example.com", "ops@beta.example.com", "ops@acme.example.com"
    ]
    assert body["items"][0]["role"] == "reviewer"
    acme = body["items"][2]
    assert acme["name"] == "Acme Ltd"
    assert acme["phone"] == "+250 788123456"
    assert acme["status"] == "active"
    assert acme["verifications"] == 2
    assert acme["active_keys"] == 1
    assert acme["last_verification_at"] is not None
    assert body["items"][1]["verifications"] == 0
    assert body["overview"] == {
        "clients": 3, "active": 3, "suspended": 0, "active_keys": 2, "verifications": 2, "staff": 1,
    }
    assert body["countries"] == ["Rwanda"]


def test_counts_use_the_status_a_client_would_see(client, db_session, clients):
    token = staff_token(client, db_session)
    rows = db_session.scalars(select(Verification).order_by(Verification.id)).all()
    rows[0].status = VerificationStatus.passed
    rows[1].expires_at = datetime.now(UTC) - timedelta(hours=1)
    db_session.commit()

    acme = listing(client, token, q="acme")["items"][0]
    assert (acme["passed"], acme["failed"], acme["needs_review"]) == (1, 0, 0)

    detail = client.get(f"/v1/console/clients/{acme['id']}", headers=auth(token)).json()
    assert detail["in_progress"] == 0  # the lapsed session counts as expired


def test_filters(client, db_session, clients):
    token = staff_token(client, db_session)
    beta = db_session.scalar(select(Account).where(Account.email == "ops@beta.example.com"))
    beta.is_active = False
    beta.country = "Kenya"
    beta.created_at = datetime(2025, 3, 1, tzinfo=UTC)
    beta.last_login_at = None
    acme = db_session.scalar(select(Account).where(Account.email == "ops@acme.example.com"))
    acme.last_login_at = datetime.now(UTC) - timedelta(days=3)
    db_session.commit()

    emails = lambda **p: [c["email"] for c in listing(client, token, **p)["items"]]  # noqa: E731
    assert emails(q="beta") == ["ops@beta.example.com"]
    assert emails(q="+250788123456") == ["ops@acme.example.com", "ops@beta.example.com"]
    assert emails(status="suspended") == ["ops@beta.example.com"]
    assert emails(status="active") == ["staff@example.com", "ops@acme.example.com"]
    assert emails(country="Kenya") == ["ops@beta.example.com"]
    assert emails(last_sign_in="week") == ["staff@example.com", "ops@acme.example.com"]
    assert emails(last_sign_in="today") == ["staff@example.com"]   # the staff account just signed in
    assert emails(last_sign_in="never") == ["ops@beta.example.com"]
    assert emails(joined_from="2025-01-01", joined_to="2025-12-31") == ["ops@beta.example.com"]
    assert listing(client, token, status="suspended")["overview"]["clients"] == 3  # the overview ignores filters
    assert client.get("/v1/console/clients", params={"status": "nope"}, headers=auth(token)).status_code == 422
    assert client.get("/v1/console/clients", params={"last_sign_in": "year"}, headers=auth(token)).status_code == 422


def test_pages(client, db_session, clients):
    token = staff_token(client, db_session)
    first = listing(client, token, page=1, page_size=1)
    second = listing(client, token, page=2, page_size=1)
    assert first["total"] == second["total"] == 3
    assert first["items"][0]["id"] != second["items"][0]["id"]


def test_options_name_every_client(client, db_session, clients):
    token = staff_token(client, db_session)
    options = client.get("/v1/console/clients/options", headers=auth(token)).json()
    assert [o["name"] for o in options] == ["Acme Ltd", "Beta Ltd"]
    assert set(options[0]) == {"id", "name", "email"}


def test_detail_shows_owner_keys_and_recent_verifications(client, db_session, clients):
    token = staff_token(client, db_session)
    acme_id = listing(client, token, q="acme")["items"][0]["id"]
    key = db_session.scalar(select(ApiKey).where(ApiKey.account_id == int(acme_id)))

    detail = client.get(f"/v1/console/clients/{acme_id}", headers=auth(token)).json()
    assert detail["first_name"] == "Acme"
    assert detail["applicants"] == 2
    assert detail["in_progress"] == 2
    assert [k["prefix"] for k in detail["api_keys"]] == [key.prefix]
    assert "secret_hash" not in detail["api_keys"][0]
    assert [v["applicant"]["name"] for v in detail["recent_verifications"]] == ["Kwame Mensah", "Amina Wanjiru"]


def test_a_staff_account_is_listed_and_opens_like_any_account(client, db_session, clients):
    token = staff_token(client, db_session)
    staff = db_session.scalar(select(Account).where(Account.email == "staff@example.com"))
    opened = client.get(f"/v1/console/clients/{staff.id}", headers=auth(token))
    assert opened.status_code == 200
    assert opened.json()["email"] == "staff@example.com"
    assert client.get("/v1/console/clients/999999", headers=auth(token)).status_code == 404


def test_clients_need_a_console_token_with_kyc(client, db_session, clients):
    _, dashboard_token = client_key(client, "ops@gamma.example.com", "Gamma")
    assert client.get("/v1/console/clients", headers=auth(dashboard_token)).status_code == 401
    assert client.get("/v1/console/clients").status_code == 401
    sign_only = staff_token(client, db_session, email="sign@example.com", products=("sign",))
    assert client.get("/v1/console/clients", headers=auth(sign_only)).status_code == 403
    assert client.get("/v1/console/clients/options", headers=auth(sign_only)).status_code == 403
