"""The client dashboard API: each account sees its own verifications, numbers,
AML screenings, API logs and keys, and nothing of any other account's.

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
    return {"acme": acme, "beta": beta, "acme_first": first, "acme_second": second, "beta_verification": other}


def get(client, token, path, **params):
    response = client.get(f"/v1/dashboard{path}", params=params, headers=auth(token))
    assert response.status_code == 200, response.text
    return response.json()


def ids(client, token) -> dict[str, str]:
    """Reference to id, for this account's verifications."""
    return {item["reference"]: item["id"] for item in get(client, token, "/verifications")["items"]}


def test_every_endpoint_needs_a_dashboard_session(client):
    for path in ("/verifications", "/metrics/overview?from=2026-01-01&to=2026-01-31", "/aml/results", "/activity", "/api-keys"):
        assert client.get(f"/v1/dashboard{path}").status_code == 401


def test_an_account_lists_only_its_own_verifications(client, two_accounts):
    acme = get(client, two_accounts["acme"], "/verifications")
    assert acme["total"] == 2
    assert {i["applicant"]["name"] for i in acme["items"]} == {"Lerato Mokoena", "Sipho Dlamini"}
    assert acme["status_counts"]["pending"] == 2
    assert {i["client"]["email"] for i in acme["items"]} == {"ops@acme.example.com"}

    beta = get(client, two_accounts["beta"], "/verifications")
    assert [i["applicant"]["name"] for i in beta["items"]] == ["Grace Uwase"]
    # Searching does not reach past the account either.
    assert get(client, two_accounts["beta"], "/verifications", q="Lerato")["total"] == 0


def test_another_accounts_verification_is_missing(client, two_accounts):
    acme_id = ids(client, two_accounts["acme"])[two_accounts["acme_first"]["reference"]]
    beta_id = ids(client, two_accounts["beta"])[two_accounts["beta_verification"]["reference"]]

    detail = get(client, two_accounts["acme"], f"/verifications/{acme_id}")
    assert detail["reference"] == two_accounts["acme_first"]["reference"]

    for path in (f"/verifications/{beta_id}", f"/verifications/{beta_id}/report", f"/verifications/{beta_id}/files/1"):
        assert client.get(f"/v1/dashboard{path}", headers=auth(two_accounts["acme"])).status_code == 404


def test_numbers_count_only_the_accounts_verifications(client, two_accounts):
    today = date.today()
    window = {"from": (today - timedelta(days=6)).isoformat(), "to": today.isoformat()}

    overview = get(client, two_accounts["acme"], "/metrics/overview", **window)
    assert overview["verifications"]["value"] == 2
    analytics = get(client, two_accounts["acme"], "/metrics/analytics", **window)
    assert analytics["clients"] == []
    assert {s["key"]: s["count"] for s in analytics["services"]} == {"document": 2, "face": 2, "aml": 2}

    assert get(client, two_accounts["beta"], "/metrics/overview", **window)["verifications"]["value"] == 1


def test_aml_lists_only_the_accounts_screenings(client, db_session, two_accounts):
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

    acme = get(client, two_accounts["acme"], "/aml/results")
    assert acme["total"] == 1
    assert acme["items"][0]["verification"]["reference"] == two_accounts["acme_first"]["reference"]
    assert acme["source_counts"]["manual"] == 0


def test_keys_are_listed_made_and_revoked_on_the_account_only(client, two_accounts):
    acme, beta = two_accounts["acme"], two_accounts["beta"]
    assert get(client, acme, "/api-keys")["total"] == 1

    made = client.post("/v1/dashboard/api-keys", json={"name": "staging", "expires_in_days": 30}, headers=auth(acme))
    assert made.status_code == 201, made.text
    assert made.json()["key"].startswith("kyc_")
    listed = get(client, acme, "/api-keys")
    assert listed["total"] == 2
    assert {k["client"]["email"] for k in listed["items"]} == {"ops@acme.example.com"}

    key_id = made.json()["id"]
    assert client.delete(f"/v1/dashboard/api-keys/{key_id}", headers=auth(beta)).status_code == 404
    revoked = client.delete(f"/v1/dashboard/api-keys/{key_id}", headers=auth(acme))
    assert revoked.status_code == 200 and revoked.json()["revoked_at"] is not None
    assert get(client, acme, "/api-keys", state="revoked")["total"] == 1


def test_api_logs_hold_the_accounts_own_events_and_hide_staff(client, db_session, two_accounts):
    acme, beta = two_accounts["acme"], two_accounts["beta"]
    client.post("/v1/dashboard/api-keys", json={"name": "staging", "expires_in_days": 30}, headers=auth(acme))

    acme_verification = db_session.scalar(
        select(Verification).where(Verification.reference == two_accounts["acme_first"]["reference"])
    )
    # A staff decision on Acme's verification, and a staff event that is not about it.
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

    events = get(client, acme, "/activity")["items"]
    types = [e["event_type"] for e in events]
    assert "api_key.created" in types
    assert "aml.search" not in types
    review = next(e for e in events if e["event_type"] == "review.approved")
    assert review["actor_name"] == "Pangolin compliance"
    assert "by" not in review["payload"]

    beta_types = [e["event_type"] for e in get(client, beta, "/activity")["items"]]
    assert "api_key.created" not in beta_types and "review.approved" not in beta_types


def test_forms_are_only_those_given_to_the_account(client, two_accounts):
    assert get(client, two_accounts["acme"], "/due-diligence/forms") == []
    assert get(client, two_accounts["acme"], "/due-diligence/responses")["total"] == 0
