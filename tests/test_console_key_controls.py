"""Console staff suspending, resuming and expiring a client's API key.

Against Postgres in a throwaway schema, like the other API tests, and skipped
when it is not reachable.
"""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.models import ApiKey, AuditEvent
from app.scripts.console_admin import create_console_account
from tests.test_console_verifications import (  # noqa: F401 (fixtures)
    PASSWORD,
    _reachable,
    auth,
    client,
    client_key,
    db_session,
    open_verification,
)

pytestmark = pytest.mark.skipif(not _reachable(), reason="needs the local Postgres")


def staff(client, db_session, role: str, email: str) -> str:
    create_console_account(
        db_session,
        email=email,
        password=PASSWORD,
        first_name="Staff",
        last_name=role.title(),
        role=role,
        products=["kyc"],
        phone="788000000",
        country_code="+250",
        country="Rwanda",
        date_of_birth=date(1990, 1, 1),
    )
    return client.post("/v1/console/auth/login", json={"email": email, "password": PASSWORD}).json()["access_token"]


@pytest.fixture()
def acme(client, db_session):
    """A client with one working key: the header to call with, and the key's id."""
    header, _ = client_key(client, "ops@acme.example.com", "Acme")
    key = db_session.scalar(select(ApiKey))
    return {"header": header, "id": key.id}


def act(client, token, key_id, action, reason=None):
    body = {"reason": reason} if reason is not None else None
    return client.post(f"/v1/console/api-keys/{key_id}/{action}", json=body, headers=auth(token))


def test_a_suspended_key_is_refused_until_it_is_resumed(client, db_session, acme):
    admin = staff(client, db_session, "admin", "admin@example.com")
    open_verification(client, acme["header"], "Amina", "Wanjiru")

    suspended = act(client, admin, acme["id"], "suspend", "Unusual traffic")
    assert suspended.status_code == 200, suspended.text
    body = suspended.json()
    assert body["state"] == "suspended"
    assert body["suspended_at"] is not None
    assert body["last_action"]["action"] == "suspended"
    assert body["last_action"]["by"] == "admin@example.com"
    assert body["last_action"]["reason"] == "Unusual traffic"

    # The client API refuses it like a wrong key.
    refused = client.post("/v1/verifications", json={"reference": "R-1", "services": ["document"]}, headers=acme["header"])
    assert refused.status_code == 401
    assert refused.json()["detail"] == "Invalid API key"

    # The client's own key list says it is not valid.
    acme_token = client.post("/v1/auth/login", json={"email": "ops@acme.example.com", "password": PASSWORD}).json()["access_token"]
    own_keys = client.get("/v1/api-keys", headers=auth(acme_token)).json()["items"]
    assert own_keys[0]["state"] == "suspended"
    assert own_keys[0]["suspended_at"] is not None

    resumed = act(client, admin, acme["id"], "resume")
    assert resumed.status_code == 200
    assert resumed.json()["state"] == "active"
    assert resumed.json()["suspended_at"] is None
    open_verification(client, acme["header"], "Kwame", "Mensah")


def test_expiring_a_key_ends_it_for_good(client, db_session, acme):
    owner = staff(client, db_session, "owner", "owner@example.com")

    expired = act(client, owner, acme["id"], "expire", "Leaked in a public repository")
    assert expired.status_code == 200, expired.text
    assert expired.json()["state"] == "expired"
    assert client.post("/v1/verifications", json={"reference": "R-2", "services": ["document"]}, headers=acme["header"]).status_code == 401

    # Nothing more can be done to it.
    assert act(client, owner, acme["id"], "expire").status_code == 409
    assert act(client, owner, acme["id"], "suspend").status_code == 409


def test_suspend_is_idempotent_and_resume_needs_a_suspended_key(client, db_session, acme):
    admin = staff(client, db_session, "admin", "admin@example.com")
    assert act(client, admin, acme["id"], "resume").status_code == 409
    assert act(client, admin, acme["id"], "suspend").status_code == 200
    assert act(client, admin, acme["id"], "suspend").status_code == 200
    events = db_session.scalars(select(AuditEvent).where(AuditEvent.event_type == "api_key.suspended")).all()
    assert len(events) == 1


def test_a_revoked_key_cannot_be_suspended_or_resumed(client, db_session, acme):
    admin = staff(client, db_session, "admin", "admin@example.com")
    assert act(client, admin, acme["id"], "suspend").status_code == 200
    key = db_session.get(ApiKey, acme["id"])
    key.revoked_at = datetime.now(UTC)
    db_session.commit()
    assert act(client, admin, acme["id"], "resume").status_code == 409


def test_suspended_keys_are_listed_and_counted_apart(client, db_session, acme):
    admin = staff(client, db_session, "admin", "admin@example.com")
    act(client, admin, acme["id"], "suspend", "Checking")

    listing = client.get("/v1/console/api-keys", headers=auth(admin)).json()
    assert listing["state_counts"]["suspended"] == 1
    assert listing["state_counts"]["active"] == 0
    assert listing["items"][0]["last_action"]["reason"] == "Checking"
    assert client.get("/v1/console/api-keys", params={"state": "suspended"}, headers=auth(admin)).json()["total"] == 1

    # A suspended key is not an active key on the client's page either.
    clients = client.get("/v1/console/clients", headers=auth(admin)).json()
    assert clients["items"][0]["active_keys"] == 0
    assert clients["overview"]["active_keys"] == 0
    detail = client.get(f"/v1/console/clients/{clients['items'][0]['id']}", headers=auth(admin)).json()
    assert detail["api_keys"][0]["suspended_at"] is not None


def test_a_suspended_key_that_also_lapsed_shows_as_expired(client, db_session, acme):
    admin = staff(client, db_session, "admin", "admin@example.com")
    act(client, admin, acme["id"], "suspend")
    key = db_session.get(ApiKey, acme["id"])
    key.expires_at = datetime.now(UTC) - timedelta(days=1)
    db_session.commit()
    listing = client.get("/v1/console/api-keys", headers=auth(admin)).json()
    assert listing["items"][0]["state"] == "expired"


@pytest.mark.parametrize("role", ["reviewer", "support", "readonly"])
def test_only_owners_and_admins_can_change_a_key(client, db_session, acme, role):
    token = staff(client, db_session, role, f"{role}@example.com")
    for action in ("suspend", "resume", "expire"):
        assert act(client, token, acme["id"], action).status_code == 403
    assert db_session.get(ApiKey, acme["id"]).suspended_at is None


def test_key_actions_need_a_console_session_and_a_real_key(client, db_session, acme):
    admin = staff(client, db_session, "admin", "admin@example.com")
    assert client.post(f"/v1/console/api-keys/{acme['id']}/suspend").status_code == 401
    assert act(client, admin, 999999, "suspend").status_code == 404
    assert act(client, admin, acme["id"], "suspend", "x" * 501).status_code == 422
