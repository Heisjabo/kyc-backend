"""The admin console's view of verifications: every client's, filtered and paged.

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
from app.db.models import DocumentScan, Step, Verification, VerificationStep, VerificationStatus
from app.db.session import get_session
from app.main import app
from app.schemas.document import Decision, DocumentType
from app.scripts.console_admin import create_console_account

PASSWORD = "horse-battery"
COMMON = {"phone": "788123456", "country_code": "+250", "date_of_birth": "1990-01-01", "country": "Rwanda"}


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


def client_key(client, email: str, first_name: str) -> tuple[dict, str]:
    """A client account with an API key; returns the key header and its dashboard token."""
    registered = client.post(
        "/v1/auth/register",
        json={"email": email, "password": PASSWORD, "first_name": first_name, "last_name": "Ltd", **COMMON},
    ).json()
    bearer = auth(registered["access_token"])
    key = client.post("/v1/api-keys", json={"name": "tests"}, headers=bearer).json()["key"]
    return {"X-API-Key": key}, registered["access_token"]


def open_verification(client, key: dict, first: str, last: str, **overrides) -> dict:
    body = {
        "reference": f"REF-{uuid.uuid4().hex[:10]}",
        "services": ["document", "face"],
        "applicant": {"first_name": first, "last_name": last, "country": "Rwanda"},
        **overrides,
    }
    response = client.post("/v1/verifications", json=body, headers=key)
    assert response.status_code == 201, response.text
    return response.json()


def staff_token(client, db_session, email="staff@example.com", products=("kyc",)) -> str:
    create_console_account(
        db_session,
        email=email,
        password=PASSWORD,
        first_name="Staff",
        last_name="Member",
        role="reviewer",
        products=list(products),
        phone="788000000",
        country_code="+250",
        country="Rwanda",
        date_of_birth=date(1990, 1, 1),
    )
    return client.post("/v1/console/auth/login", json={"email": email, "password": PASSWORD}).json()["access_token"]


@pytest.fixture()
def two_clients(client):
    """Two clients, Acme with two verifications and Beta with one, opened in that order."""
    acme, _ = client_key(client, "ops@acme.example.com", "Acme")
    beta, _ = client_key(client, "ops@beta.example.com", "Beta")
    first = open_verification(client, acme, "Amina", "Wanjiru")
    second = open_verification(client, acme, "Kwame", "Mensah")
    third = open_verification(client, beta, "Grace", "Uwase")
    return {"acme_first": first, "acme_second": second, "beta": third}


def listing(client, token, **params):
    response = client.get("/v1/console/verifications", params=params, headers=auth(token))
    assert response.status_code == 200, response.text
    return response.json()


# Listing

def test_staff_see_every_clients_verifications_newest_first(client, db_session, two_clients):
    token = staff_token(client, db_session)
    body = listing(client, token)
    assert body["total"] == 3
    assert [item["reference"] for item in body["items"]] == [
        two_clients["beta"]["reference"],
        two_clients["acme_second"]["reference"],
        two_clients["acme_first"]["reference"],
    ]
    first = body["items"][0]
    assert first["client"]["name"] == "Beta Ltd"
    assert first["client"]["email"] == "ops@beta.example.com"
    assert first["applicant"]["name"] == "Grace Uwase"
    assert first["applicant"]["customer_id"] == two_clients["beta"]["customer_id"]
    assert first["status"] == "pending"
    assert body["status_counts"]["pending"] == 3


def test_filtering_by_client_search_and_status(client, db_session, two_clients):
    token = staff_token(client, db_session)
    acme_id = listing(client, token, q="Amina")["items"][0]["client"]["id"]

    assert listing(client, token, client_id=acme_id)["total"] == 2
    assert [i["applicant"]["name"] for i in listing(client, token, q="kwame")["items"]] == ["Kwame Mensah"]
    by_reference = listing(client, token, q=two_clients["beta"]["reference"])
    assert by_reference["total"] == 1
    assert listing(client, token, status="passed")["total"] == 0
    assert client.get("/v1/console/verifications", params={"status": "nonsense"}, headers=auth(token)).status_code == 422


def test_an_open_session_past_its_expiry_is_listed_as_expired_without_writing(client, db_session, two_clients):
    token = staff_token(client, db_session)
    row = db_session.scalar(select(Verification).where(Verification.reference == two_clients["acme_first"]["reference"]))
    row.expires_at = datetime.now(UTC) - timedelta(hours=1)
    db_session.commit()

    body = listing(client, token)
    assert body["status_counts"]["expired"] == 1
    assert body["status_counts"]["pending"] == 2
    expired = listing(client, token, status="expired")
    assert [i["reference"] for i in expired["items"]] == [two_clients["acme_first"]["reference"]]
    db_session.refresh(row)
    assert row.status == VerificationStatus.pending


def test_pages(client, db_session, two_clients):
    token = staff_token(client, db_session)
    first = listing(client, token, page=1, page_size=2)
    second = listing(client, token, page=2, page_size=2)
    assert first["total"] == second["total"] == 3
    assert len(first["items"]) == 2 and len(second["items"]) == 1
    assert {i["id"] for i in first["items"]}.isdisjoint({i["id"] for i in second["items"]})


def test_created_date_range(client, db_session, two_clients):
    token = staff_token(client, db_session)
    today = datetime.now(UTC).date()
    assert listing(client, token, created_from=today.isoformat(), created_to=today.isoformat())["total"] == 3
    assert listing(client, token, created_to=(today - timedelta(days=1)).isoformat())["total"] == 0


# One verification

def test_one_verification_with_its_steps_and_document(client, db_session, two_clients):
    token = staff_token(client, db_session)
    row = db_session.scalar(select(Verification).where(Verification.reference == two_clients["acme_second"]["reference"]))
    step = db_session.scalar(select(Step).where(Step.verification_id == row.id, Step.step == VerificationStep.document))
    db_session.add(
        DocumentScan(
            step_id=step.id,
            verification_id=row.id,
            document_type=DocumentType.passport,
            decision=Decision.passed,
            full_name="KWAME MENSAH",
            document_number="G12345678",
            nationality="GHA",
            expiry_date=date(2030, 5, 1),
        )
    )
    db_session.commit()

    response = client.get(f"/v1/console/verifications/{row.id}", headers=auth(token))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reference"] == row.reference
    assert body["client"]["name"] == "Acme Ltd"
    assert [s["step"] for s in body["steps"]] == ["document", "face"]
    assert body["document_type"] == "passport"
    assert body["document"]["full_name"] == "KWAME MENSAH"
    assert body["document"]["document_number_last4"] == "5678"
    assert "G12345678" not in response.text


def test_an_unknown_id_is_not_found(client, db_session):
    token = staff_token(client, db_session)
    assert client.get("/v1/console/verifications/999999", headers=auth(token)).status_code == 404


# Who may look

def test_only_staff_with_kyc_access_may_look(client, db_session, two_clients):
    _, client_token = client_key(client, "ops@gamma.example.com", "Gamma")
    sign_only = staff_token(client, db_session, email="signer@example.com", products=("sign",))

    assert client.get("/v1/console/verifications").status_code == 401
    assert client.get("/v1/console/verifications", headers=auth(client_token)).status_code == 401
    assert client.get("/v1/console/verifications", headers=auth(sign_only)).status_code == 403
