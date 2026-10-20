"""A child verified on the back of a verified parent.

The certificate is drawn here rather than photographed, so these test the rules
rather than the OCR, but the text is read by the real reader and matched by the
real matcher.
"""

import uuid

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.db.base import Base
from app.db.models import Verification, VerificationFile
from app.db.session import get_session
from app.main import app
from app.schemas.document import CheckResult, CheckStatus, Decision, DocumentCheckResponse, ExtractedFields

ACCOUNT = {
    "email": "children@example.com", "password": "horse-battery", "first_name": "Jean",
    "last_name": "Mugisha", "phone": "788123456", "country_code": "+250",
    "date_of_birth": "1995-04-12", "country": "Rwanda",
}


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


@pytest.fixture()
def api_key(client) -> dict:
    registered = client.post("/v1/auth/register", json=ACCOUNT).json()
    bearer = {"Authorization": f"Bearer {registered['access_token']}"}
    made = client.post("/v1/api-keys", json={"name": "tests"}, headers=bearer).json()
    return {"X-API-Key": made["key"]}


def certificate(child="UWASE DIVINE", father="JABO JEAN MARIE",
                country="REPUBLIC OF RWANDA", dob="12-06-2018", heading="BIRTH CERTIFICATE") -> bytes:
    image = np.full((900, 1400, 3), 245, dtype=np.uint8)

    def line(textual, y, scale=1.0, thickness=2):
        cv2.putText(image, textual, (70, y), cv2.FONT_HERSHEY_SIMPLEX, scale, (20, 20, 20), thickness, cv2.LINE_AA)

    line(country, 90, 1.3, 3)
    line(heading, 170, 1.5, 3)
    line("Names: " + child, 300)
    line("Sex: F", 370)
    line("Date of birth: " + dob, 440)
    line("Place of birth: KIGALI", 510)
    line("Father: " + father, 580)
    line("Mother: MUKAMANA ALICE", 650)
    line("Registered: 20-06-2018", 720)
    return cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])[1].tobytes()


def passed_parent(client, api_key, monkeypatch, reference="PARENT-1"):
    """A parent whose document step passed, with a verified name and country."""
    from app.api.v1.routes import session_steps

    def fake_scan(document_type, front, back=None, applicant=None, today=None, require_adult=True):
        return DocumentCheckResponse(
            decision=Decision.passed, reasons=[], warnings=[], document_type=document_type,
            extracted=ExtractedFields(source="visual", surname="JABO", given_names="JEAN MARIE",
                                      issuing_country="RWA", document_number="1200280158390063"),
            checks=[CheckResult(name="document_detection", status=CheckStatus.passed)], processing_ms=3,
        )

    monkeypatch.setattr(session_steps, "scan_document", fake_scan)
    monkeypatch.setattr(session_steps, "_keep_document_portrait", lambda *a: "ok")

    opened = client.post("/v1/verifications", headers=api_key, json={
        "reference": reference, "customer_id": "parent-1", "services": ["document"],
        "applicant": {"first_name": "Jean Marie", "last_name": "Jabo", "country": "Rwanda"},
    }).json()
    token = opened["session_url"].rsplit("/", 1)[-1]
    client.post(f"/v1/sessions/{token}/document", data={"document_type": "id_card"},
                files={"front": ("front.jpg", certificate(), "image/jpeg")})
    return opened


def add_child(client, api_key, cert=None, reference="PARENT-1", **form):
    return client.post(
        f"/v1/verifications/{reference}/children", headers=api_key,
        data={"first_name": "Divine", "last_name": "Uwase", "date_of_birth": "2018-06-12", **form},
        files={"certificate": ("certificate.jpg", cert or certificate(), "image/jpeg")},
    )


def test_a_child_passes_on_a_good_certificate(client, api_key, monkeypatch):
    passed_parent(client, api_key, monkeypatch)
    body = add_child(client, api_key).json()
    assert body["decision"] == "pass" and body["status"] == "passed"
    assert body["parent_reference"] == "PARENT-1"
    named = {check["name"]: check["status"] for check in body["checks"]}
    assert named["parent_named_on_certificate"] == "pass"
    assert named["certificate_country"] == "pass"
    assert named["name_match"] == "pass"
    assert named["date_of_birth_match"] == "pass"


def test_the_parent_must_be_named_on_the_certificate(client, api_key, monkeypatch):
    """A guardian who is on neither side cannot be linked by this document."""
    passed_parent(client, api_key, monkeypatch)
    body = add_child(client, api_key, certificate(father="NKURUNZIZA PATRICK")).json()
    assert body["decision"] == "fail"
    assert "PARENT_NOT_ON_CERTIFICATE" in body["reasons"]


def test_a_child_born_abroad_goes_to_a_person(client, api_key, monkeypatch):
    """A family living abroad has a genuine certificate from another country.
    Refusing that outright costs a real customer, so it is a review."""
    passed_parent(client, api_key, monkeypatch)
    body = add_child(client, api_key, certificate(country="REPUBLIC OF KENYA")).json()
    assert body["decision"] == "review"
    assert "CERTIFICATE_COUNTRY_MISMATCH" in body["reasons"]


def test_the_childs_date_of_birth_has_to_match(client, api_key, monkeypatch):
    passed_parent(client, api_key, monkeypatch)
    body = add_child(client, api_key, certificate(dob="01-01-2015")).json()
    assert body["decision"] == "fail"
    assert "DOB_MISMATCH" in body["reasons"]


def test_the_childs_name_has_to_be_on_it(client, api_key, monkeypatch):
    passed_parent(client, api_key, monkeypatch)
    body = add_child(client, api_key, certificate(child="KEZA ALINE")).json()
    assert body["decision"] in ("fail", "review")
    assert any("NAME" in reason for reason in body["reasons"])


def test_the_age_rule_does_not_apply(client, api_key, monkeypatch):
    """The whole point: a child is under 18 and that is not a failure."""
    passed_parent(client, api_key, monkeypatch)
    body = add_child(client, api_key).json()
    assert "UNDERAGE" not in body["reasons"]


def test_a_child_needs_a_parent_who_passed(client, api_key, monkeypatch):
    opened = client.post("/v1/verifications", headers=api_key, json={
        "reference": "PENDING-1", "services": ["document"],
        "applicant": {"first_name": "Jean Marie", "last_name": "Jabo", "country": "Rwanda"}}).json()
    response = add_child(client, api_key, reference=opened["reference"])
    assert response.status_code == 409
    assert "passed" in response.json()["detail"]


def test_an_unknown_parent_is_not_found(client, api_key):
    assert add_child(client, api_key, reference="NO-SUCH-REF").status_code == 404


def test_adding_a_child_needs_the_api_key(client, api_key, monkeypatch):
    passed_parent(client, api_key, monkeypatch)
    response = client.post("/v1/verifications/PARENT-1/children",
                           data={"first_name": "Divine", "last_name": "Uwase", "date_of_birth": "2018-06-12"},
                           files={"certificate": ("c.jpg", certificate(), "image/jpeg")})
    assert response.status_code == 401


def test_one_client_cannot_add_a_child_to_anothers_parent(client, api_key, monkeypatch):
    passed_parent(client, api_key, monkeypatch)
    other = client.post("/v1/auth/register", json={**ACCOUNT, "email": "other@example.com"}).json()
    bearer = {"Authorization": f"Bearer {other['access_token']}"}
    theirs = {"X-API-Key": client.post("/v1/api-keys", json={"name": "t"}, headers=bearer).json()["key"]}
    assert add_child(client, theirs).status_code == 404


def test_the_child_is_its_own_verification_linked_to_the_parent(client, api_key, monkeypatch, db_session):
    parent = passed_parent(client, api_key, monkeypatch)
    body = add_child(client, api_key).json()

    child = db_session.scalar(select(Verification).where(Verification.reference == body["reference"]))
    mother_row = db_session.scalar(select(Verification).where(Verification.reference == parent["reference"]))
    assert child.parent_verification_id == mother_row.id
    assert child.applicant_id != mother_row.applicant_id

    files = db_session.scalars(select(VerificationFile).where(VerificationFile.verification_id == child.id)).all()
    assert [f.kind.value for f in files] == ["document_front"]
