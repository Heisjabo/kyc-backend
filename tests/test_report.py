"""The verification report: a PDF for any verification, whatever its status.

Run against Postgres in a throwaway schema, with the engines stubbed, the same
way the session tests are: what is under test is what the report reads off the
rows, and that asking for it changes nothing.
"""

from datetime import UTC, datetime, timedelta

import pypdfium2 as pdfium
from sqlalchemy import select

from app.db.models import (
    CallbackDelivery,
    DeviceSession,
    FaceCheck,
    FileKind,
    Step,
    StepStatus,
    Verification,
    VerificationFile,
    VerificationStatus,
    VerificationStep,
)
from app.schemas.document import Decision
from app.storage import evidence
from tests.test_console_verifications import auth, staff_token
from tests.test_sessions import (  # noqa: F401  (fixtures)
    api_key,
    client,
    db_session,
    image_bytes,
    open_verification,
    stub_document,
    token_of,
)


def pdf_text(data: bytes) -> str:
    document = pdfium.PdfDocument(data)
    try:
        return "\n".join(document[i].get_textpage().get_text_bounded() for i in range(len(document)))
    finally:
        document.close()


def fetch(client, api_key, reference: str):
    return client.get(f"/v1/verifications/{reference}/report", headers=api_key)


def row(db_session, reference: str) -> Verification:
    return db_session.scalar(select(Verification).where(Verification.reference == reference))


def test_the_report_is_a_pdf_named_the_way_the_shufti_proof_is(client, api_key):
    body = open_verification(client, api_key)
    response = fetch(client, api_key, body["reference"])
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == (
        f'attachment; filename="verification_{body["reference"]}.pdf"'
    )
    assert response.content.startswith(b"%PDF-")


def test_a_verification_nobody_started_reads_as_pending(client, api_key):
    body = open_verification(client, api_key)
    text = pdf_text(fetch(client, api_key, body["reference"]).content)
    assert "Verification has been created and has not been started" in text
    assert f"Reference: {body['reference']}" in text and "Reference ID" not in text
    assert body["customer_id"] in text
    assert "Document Originality (Not performed)" in text
    assert "Liveness Not Performed" in text


def test_a_declined_verification_names_its_reasons(client, api_key, monkeypatch):
    stub_document(monkeypatch, decision=Decision.failed, reasons=["DOB_MISMATCH"])
    body = open_verification(client, api_key)
    client.post(f"/v1/sessions/{token_of(body)}/document", data={"document_type": "id_card"},
                files={"front": ("front.jpg", image_bytes(), "image/jpeg")})

    text = pdf_text(fetch(client, api_key, body["reference"]).content)
    assert "Verification has been completed and declined" in text
    assert "Declined" in text
    assert "DOB_MISMATCH" in text
    assert "1199980012345678" in text


def test_a_session_past_its_expiry_reads_as_expired_without_being_moved(client, api_key, db_session):
    body = open_verification(client, api_key)
    verification = row(db_session, body["reference"])
    verification.expires_at = datetime.now(UTC) - timedelta(hours=1)
    db_session.commit()

    text = pdf_text(fetch(client, api_key, body["reference"]).content)
    assert "Verification link expired before the verification was completed" in text
    db_session.refresh(verification)
    assert verification.status == VerificationStatus.pending


def test_a_passed_verification_shows_every_section(client, api_key, db_session, tmp_path):
    body = open_verification(client, api_key, callback_url="https://client.example.com/kyc")
    verification = row(db_session, body["reference"])
    steps = {s.step: s for s in verification.steps}

    steps[VerificationStep.document].status = StepStatus.passed
    steps[VerificationStep.document].extracted = {
        "document_type": "passport", "source": "mrz", "surname": "MUGISHA", "given_names": "JEAN",
        "document_number": "PC1234567", "issuing_country": "RWA", "nationality": "RWA",
        "date_of_birth": "1995-04-12", "expiry_date": "2030-01-01", "sex": "M",
    }
    steps[VerificationStep.document].result = {"checks": [
        {"name": "document_detection", "status": "pass"},
        {"name": "date_of_birth_match", "status": "pass"},
        {"name": "validity", "status": "pass"},
    ]}
    steps[VerificationStep.document].device = {"ip": "41.186.0.1", "user_agent":
        "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36"}
    for name in (VerificationStep.face, VerificationStep.aml):
        steps[name].status = StepStatus.passed
    steps[VerificationStep.aml].result = {"outcome": "no_match", "action": "allow", "dataset": "sanctions",
                                          "highest_score": 0.12, "matches": []}
    verification.status = VerificationStatus.passed
    verification.event = "verification.accepted"

    db_session.add(FaceCheck(step_id=steps[VerificationStep.face].id, verification_id=verification.id,
                             liveness_passed=True, challenge_completed=["left", "right"], planarity=3.1,
                             deepfake_checked=True, deepfake_passed=True, deepfake_score=0.01,
                             match_score=0.76, match_threshold=0.4, matched=True))
    for kind in (FileKind.document_front, FileKind.portrait):
        stored = evidence.keep(verification.id, image_bytes(), kind.value)
        db_session.add(VerificationFile(verification_id=verification.id, kind=kind,
                                        storage_key=stored.storage_key, content_type=stored.content_type,
                                        byte_size=stored.byte_size, sha256=stored.sha256))
    db_session.add(DeviceSession(verification_id=verification.id, ip="41.186.0.1", country="Rwanda",
                                 city="Kigali", is_proxy=False, risk_level="Low", fraud_score=0))
    db_session.add(CallbackDelivery(verification_id=verification.id, event="verification.accepted",
                                    attempt=2, url=verification.callback_url, http_status=200))
    db_session.commit()

    text = pdf_text(fetch(client, api_key, body["reference"]).content)
    for expected in (
        "Verification has been successfully completed and accepted", "Accepted", "Liveness Passed",
        "Facial Biometrics Verification", "Document Verification", "AML Screening",
        "MRZ Data Breakdown", "PC1234567", "1995-04-12 (Matched)", "2030-01-01",
        "Verification Settings", "Callback Details", "verification.accepted", "200",
        "Face Match Score:", "0.7600", "pass at 0.40 or above", "Pangolin KYC", "Document Front Side", "Face",
        "Device and Network Intelligence", "Kigali", "Android 14", "Chrome 140.0.0.0",
    ):
        assert expected in text, expected


def test_another_clients_reference_is_not_found(client, api_key):
    body = open_verification(client, api_key)
    other = client.post("/v1/auth/register", json={
        "email": "other@tuesday.africa", "password": "horse-battery", "first_name": "O", "last_name": "C",
        "phone": "788000111", "country_code": "+250", "date_of_birth": "1990-01-01", "country": "Rwanda",
    }).json()
    other_key = client.post("/v1/api-keys", json={"name": "t"},
                            headers={"Authorization": f"Bearer {other['access_token']}"}).json()["key"]
    assert fetch(client, {"X-API-Key": other_key}, body["reference"]).status_code == 404
    assert client.get(f"/v1/verifications/{body['reference']}/report").status_code == 401


def test_staff_can_download_any_report_from_the_console(client, api_key, db_session):
    body = open_verification(client, api_key)
    verification = row(db_session, body["reference"])
    token = staff_token(client, db_session)

    response = client.get(f"/v1/console/verifications/{verification.id}/report", headers=auth(token))
    assert response.status_code == 200 and response.content.startswith(b"%PDF-")
    assert client.get("/v1/console/verifications/999999/report", headers=auth(token)).status_code == 404
