"""The shared read endpoints, scoped by who is asking: a client, signed in to the
dashboard or with an API key, sees its own account's data and nothing of any
other account's; Pangolin staff, signed in to the console, see every client's.

Against Postgres in a throwaway schema, like the other API tests, and skipped
when it is not reachable.
"""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.db.base import Base
from app.db.models import AuditEvent, Step, StepStatus, Verification, VerificationStep
from app.db.session import get_session
from app.main import app
from app.scripts.console_admin import create_console_account

PASSWORD = "horse-battery"
COMMON = {"phone": "821234567", "country_code": "+27", "date_of_birth": "1990-01-01", "country": "South Africa"}


def _reachable() -> bool:
    try:
        create_engine(settings.DATABASE_URL).connect().close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="needs the local Postgres")


@pytest.fixture()
def db_session():
    schema = f"test_{uuid.uuid4().hex[:12]}"
    engine = create_engine(settings.DATABASE_URL, connect_args={"options": f"-csearch_path={schema}"})
    admin = create_engine(settings.DATABASE_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture()
def client(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "EVIDENCE_DIR", str(tmp_path / "evidence"))
    app.dependency_overrides[get_session] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def account(client, email: str, first_name: str) -> tuple[dict, str]:
    """A client account with an API key: the key header and the dashboard token."""
    registered = client.post(
        "/v1/auth/register",
        json={"email": email, "password": PASSWORD, "first_name": first_name, "last_name": "Ltd", **COMMON},
    ).json()
    token = registered["access_token"]
    key = client.post("/v1/api-keys", json={"name": "server"}, headers=auth(token)).json()["key"]
    return {"X-API-Key": key}, token


def staff(client, db_session, role="admin", email="staff@pangolin.example.com") -> str:
    create_console_account(
        db_session,
        email=email,
        password=PASSWORD,
        first_name="Staff",
        last_name="Member",
        role=role,
        products=["kyc"],
        phone="788000000",
        country_code="+250",
        country="Rwanda",
        date_of_birth=date(1990, 1, 1),
    )
    return client.post("/v1/console/auth/login", json={"email": email, "password": PASSWORD}).json()["access_token"]


def open_verification(client, key: dict, first: str, last: str) -> dict:
    body = {
        "reference": f"REF-{uuid.uuid4().hex[:10]}",
        "services": ["document", "face", "aml"],
        "applicant": {"first_name": first, "last_name": last, "country": "South Africa"},
    }
    response = client.post("/v1/verifications", json=body, headers=key)
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture()
def two_accounts(client):
    """Acme with two verifications and Beta with one."""
    acme_key, acme = account(client, "ops@acme.example.com", "Acme")
    beta_key, beta = account(client, "ops@beta.example.com", "Beta")
    first = open_verification(client, acme_key, "Lerato", "Mokoena")
    second = open_verification(client, acme_key, "Sipho", "Dlamini")
    other = open_verification(client, beta_key, "Grace", "Uwase")
    return {
        "acme": acme,
        "acme_key": acme_key,
        "beta": beta,
        "acme_first": first,
        "acme_second": second,
        "beta_verification": other,
    }


def get(client, headers, path, **params):
    response = client.get(f"/v1{path}", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_the_shared_reads_need_someone_signed_in(client):
    for path in ("/verifications", "/metrics/overview?from=2026-01-01&to=2026-01-31", "/aml/results", "/audit-events", "/api-keys"):
        assert client.get(f"/v1{path}").status_code == 401


def test_a_client_lists_only_its_own_verifications(client, two_accounts):
    acme = get(client, auth(two_accounts["acme"]), "/verifications")
    assert acme["total"] == 2
    assert {i["applicant"]["name"] for i in acme["items"]} == {"Lerato Mokoena", "Sipho Dlamini"}
    assert {i["client"]["email"] for i in acme["items"]} == {"ops@acme.example.com"}

    # The same with its API key, and asking for another client gets it nothing more.
    assert get(client, two_accounts["acme_key"], "/verifications")["total"] == 2
    beta_id = get(client, auth(two_accounts["beta"]), "/verifications")["items"][0]["client"]["id"]
    assert get(client, auth(two_accounts["acme"]), "/verifications", client_id=beta_id)["total"] == 2
    assert get(client, auth(two_accounts["beta"]), "/verifications", q="Lerato")["total"] == 0


def test_staff_see_every_clients_verifications(client, db_session, two_accounts):
    token = staff(client, db_session)
    everyone = get(client, auth(token), "/verifications")
    assert everyone["total"] == 3
    acme_id = next(i["client"]["id"] for i in everyone["items"] if i["client"]["email"] == "ops@acme.example.com")
    assert get(client, auth(token), "/verifications", client_id=acme_id)["total"] == 2

    reference = two_accounts["beta_verification"]["reference"]
    # A reference is only unique within one client, so staff name the client.
    assert client.get(f"/v1/verifications/{reference}", headers=auth(token)).status_code == 422
    beta_id = next(i["client"]["id"] for i in everyone["items"] if i["client"]["email"] == "ops@beta.example.com")
    assert get(client, auth(token), f"/verifications/{reference}", client_id=beta_id)["reference"] == reference


def test_one_verification_in_full_and_another_clients_is_missing(client, two_accounts):
    mine = two_accounts["acme_first"]["reference"]
    detail = get(client, auth(two_accounts["acme"]), f"/verifications/{mine}")
    # Everything the endpoint has always returned, and the full view beside it.
    assert detail["reference"] == mine and detail["session_url"] and detail["customer_id"]
    assert detail["applicant"]["name"] == "Lerato Mokoena"
    assert [s["step"] for s in detail["steps"]] == ["document", "face", "aml"]
    assert detail["evidence"] == []
    # An API key reads the same.
    assert get(client, two_accounts["acme_key"], f"/verifications/{mine}")["id"] == detail["id"]

    theirs = two_accounts["beta_verification"]["reference"]
    for path in (f"/verifications/{theirs}", f"/verifications/{theirs}/report", f"/verifications/{theirs}/evidence/1"):
        assert client.get(f"/v1{path}", headers=auth(two_accounts["acme"])).status_code == 404


def test_numbers_count_the_callers_verifications(client, db_session, two_accounts):
    today = date.today()
    window = {"from": (today - timedelta(days=6)).isoformat(), "to": today.isoformat()}

    assert get(client, auth(two_accounts["acme"]), "/metrics/overview", **window)["verifications"]["value"] == 2
    analytics = get(client, auth(two_accounts["acme"]), "/metrics/analytics", **window)
    assert analytics["clients"] == []
    assert {s["key"]: s["count"] for s in analytics["services"]} == {"document": 2, "face": 2, "aml": 2}
    assert get(client, auth(two_accounts["beta"]), "/metrics/overview", **window)["verifications"]["value"] == 1

    token = staff(client, db_session)
    assert get(client, auth(token), "/metrics/overview", **window)["verifications"]["value"] == 3
    assert len(get(client, auth(token), "/metrics/analytics", **window)["clients"]) == 2


def test_aml_lists_the_callers_screenings(client, db_session, two_accounts):
    for reference in (two_accounts["acme_first"]["reference"], two_accounts["beta_verification"]["reference"]):
        verification = db_session.scalar(select(Verification).where(Verification.reference == reference))
        # Opening the verification made its AML step, waiting; screen it as a possible match.
        step = db_session.scalar(
            select(Step).where(Step.verification_id == verification.id, Step.step == VerificationStep.aml)
        )
        step.status = StepStatus.review
        step.result = {
            "outcome": "possible_match",
            "action": "review",
            "highest_score": 0.8,
            "dataset": "default",
            "screened_at": datetime.now(UTC).isoformat(),
            "candidates": [],
        }
        step.completed_at = datetime.now(UTC)
    db_session.commit()

    acme = get(client, auth(two_accounts["acme"]), "/aml/results")
    assert acme["total"] == 1
    assert acme["items"][0]["verification"]["reference"] == two_accounts["acme_first"]["reference"]
    assert get(client, auth(staff(client, db_session)), "/aml/results")["total"] == 2


def test_keys_are_listed_made_and_revoked_on_the_callers_account(client, db_session, two_accounts):
    acme, beta = two_accounts["acme"], two_accounts["beta"]
    assert get(client, auth(acme), "/api-keys")["total"] == 1

    made = client.post("/v1/api-keys", json={"name": "staging", "expires_in_days": 30}, headers=auth(acme))
    assert made.status_code == 201, made.text
    listed = get(client, auth(acme), "/api-keys")
    assert listed["total"] == 2
    assert {k["client"]["email"] for k in listed["items"]} == {"ops@acme.example.com"}

    key_id = made.json()["id"]
    assert client.delete(f"/v1/api-keys/{key_id}", headers=auth(beta)).status_code == 404
    assert client.delete(f"/v1/api-keys/{key_id}", headers=auth(acme)).json()["revoked_at"] is not None
    assert get(client, auth(acme), "/api-keys", state="revoked")["total"] == 1

    # Staff list every client's keys, and make theirs in the console, not here.
    token = staff(client, db_session)
    assert get(client, auth(token), "/api-keys")["total"] == 3
    assert client.post("/v1/api-keys", json={"name": "x"}, headers=auth(token)).status_code == 401


def test_api_logs_hold_the_clients_own_events_and_hide_staff(client, db_session, two_accounts):
    acme, beta = two_accounts["acme"], two_accounts["beta"]
    client.post("/v1/api-keys", json={"name": "staging", "expires_in_days": 30}, headers=auth(acme))

    acme_verification = db_session.scalar(
        select(Verification).where(Verification.reference == two_accounts["acme_first"]["reference"])
    )
    db_session.add(
        AuditEvent(
            event_type="review.approved",
            actor="999",
            verification_id=acme_verification.id,
            payload={"by": "reviewer@pangolin.example.com", "note": "Looks right"},
        )
    )
    db_session.add(AuditEvent(event_type="aml.search", actor="999", payload={"query": {"full_name": "Someone"}}))
    db_session.commit()

    events = get(client, auth(acme), "/audit-events")["items"]
    types = [e["event_type"] for e in events]
    assert "api_key.created" in types
    assert "aml.search" not in types
    review = next(e for e in events if e["event_type"] == "review.approved")
    assert review["actor_name"] == "Pangolin compliance"
    assert review["verification_reference"] == two_accounts["acme_first"]["reference"]
    assert "by" not in review["payload"]

    beta_events = get(client, auth(beta), "/audit-events")["items"]
    assert not [e for e in beta_events if e["payload"].get("name") == "staging"]
    assert "review.approved" not in [e["event_type"] for e in beta_events]

    # The whole log is for console owners and admins only.
    assert client.get("/v1/audit-events", headers=auth(staff(client, db_session, role="reviewer"))).status_code == 403
    admin = staff(client, db_session, email="admin@pangolin.example.com")
    assert "aml.search" in [e["event_type"] for e in get(client, auth(admin), "/audit-events")["items"]]


def test_forms_are_only_those_given_to_the_client(client, two_accounts):
    assert get(client, auth(two_accounts["acme"]), "/due-diligence/forms") == []
    assert get(client, two_accounts["acme_key"], "/due-diligence/forms") == []
    assert get(client, auth(two_accounts["acme"]), "/due-diligence/responses")["total"] == 0
