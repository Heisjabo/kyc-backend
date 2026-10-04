"""Opening a verification and walking through it.

These run against Postgres in a throwaway schema, because that is what the
service runs on and because the whole point of these tests is what gets written
down. The document and face engines are stubbed: what is under test is the
session, not the models, which have their own tests.
"""

import io
import uuid
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.db.base import Base
from app.db.models import (
    FaceCheck,
    FileKind,
    Step,
    StepStatus,
    Verification,
    VerificationFile,
    VerificationStatus,
)
from app.db.session import get_session
from app.main import app
from app.schemas.document import CheckResult, CheckStatus, Decision, DocumentCheckResponse, ExtractedFields

ACCOUNT = {
    "email": "sessions@tuesday.africa",
    "password": "horse-battery",
    "first_name": "Jean",
    "last_name": "Mugisha",
    "phone": "788123456",
    "country_code": "+250",
    "date_of_birth": "1995-04-12",
    "country": "Rwanda",
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
    # Evidence goes to a folder of this test's own.
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


def image_bytes(width: int = 640, height: int = 400) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(np.full((height, width, 3), 200, dtype=np.uint8)).save(buffer, format="JPEG")
    return buffer.getvalue()


def open_verification(client, api_key, **overrides) -> dict:
    body = {
        "reference": f"REF-{uuid.uuid4().hex[:10]}",
        "services": ["document", "face"],
        "applicant": {"first_name": "Jean", "last_name": "Mugisha", "country": "Rwanda"},
        **overrides,
    }
    return client.post("/v1/verifications", json=body, headers=api_key).json()


def token_of(verification: dict) -> str:
    return verification["session_url"].rsplit("/", 1)[-1]


def stub_document(monkeypatch, decision=Decision.passed, reasons=None, stub_portrait=True):
    from app.api.v1.routes import session_steps

    def fake_scan(document_type, front, back=None, applicant=None, today=None, require_adult=True):
        return DocumentCheckResponse(
            decision=decision,
            reasons=reasons or [],
            warnings=[],
            document_type=document_type,
            extracted=ExtractedFields(
                source="visual", surname="MUGISHA", given_names="JEAN",
                document_number="1199980012345678", issuing_country="RWA",
            ),
            checks=[CheckResult(name="document_detection", status=CheckStatus.passed)],
            processing_ms=5,
        )

    monkeypatch.setattr(session_steps, "scan_document", fake_scan)
    if stub_portrait:
        monkeypatch.setattr(session_steps, "_keep_document_portrait",
                            lambda db, verification, step, front: "ok")


# Opening one

def test_opening_a_verification_creates_the_row_and_its_steps(client, api_key, db_session):
    body = open_verification(client, api_key)
    assert body["status"] == "pending" and body["event"] == "request.received"
    assert [step["step"] for step in body["steps"]] == ["document", "face", "aml"]
    assert all(step["status"] == "pending" for step in body["steps"])
    assert body["session_url"].endswith(token_of(body))
    assert body["customer_id"]


def test_the_same_reference_cannot_be_used_twice(client, api_key):
    first = open_verification(client, api_key)
    again = client.post("/v1/verifications", headers=api_key,
                        json={"reference": first["reference"], "services": ["document"]})
    assert again.status_code == 409


def test_every_verification_opens_its_own_applicant(client, api_key):
    """A client sends one reference per verification and nothing that names the
    person, the way we send Shufti nothing but a reference. So two attempts by
    the same human are two applicants, and the face seen twice is what links
    them: the duplicate warning, never an automatic merge."""
    first = open_verification(client, api_key)
    second = open_verification(client, api_key)
    assert first["customer_id"] != second["customer_id"]


def test_the_customer_id_comes_back_without_anybody_sending_one(client, api_key):
    """Ours, generated, the way Shufti puts a Customer ID on their report."""
    body = open_verification(client, api_key)
    assert body["customer_id"] and len(body["customer_id"]) == 22


def test_a_session_can_be_asked_to_last_a_week(client, api_key):
    body = open_verification(client, api_key, ttl_days=7)
    expires = datetime.fromisoformat(body["expires_at"])
    assert timedelta(days=6, hours=23) < expires - datetime.now(expires.tzinfo) <= timedelta(days=7)


def test_a_session_cannot_outlive_the_ceiling(client, api_key):
    response = client.post("/v1/verifications", headers=api_key,
                           json={"reference": "TOO-LONG-1", "ttl_days": 31, "services": ["document"]})
    assert response.status_code == 422


def test_opening_one_needs_an_api_key(client):
    assert client.post("/v1/verifications", json={"services": ["document"]}).status_code == 401


# The session as the person sees it

def test_the_session_view_says_what_is_next(client, api_key):
    body = open_verification(client, api_key)
    session = client.get(f"/v1/sessions/{token_of(body)}").json()
    assert session["next_step"] == "document"
    assert session["completed_steps"] == [] and session["expired"] is False


def test_the_session_view_carries_nothing_about_the_client(client, api_key):
    """Anybody holding the link can read it, so it must not leak."""
    body = open_verification(client, api_key)
    session = client.get(f"/v1/sessions/{token_of(body)}").json()
    assert set(session) == {"reference", "status", "services", "next_step",
                            "completed_steps", "expired", "redirect_url"}


def test_an_unknown_link_is_not_found(client):
    assert client.get("/v1/sessions/made-up-token").status_code == 404


# Walking through it

def test_the_document_step_is_recorded_with_its_files(client, api_key, db_session, monkeypatch):
    stub_document(monkeypatch)
    body = open_verification(client, api_key)
    response = client.post(
        f"/v1/sessions/{token_of(body)}/document",
        data={"document_type": "id_card"},
        files={"front": ("front.jpg", image_bytes(), "image/jpeg"),
               "back": ("back.jpg", image_bytes(), "image/jpeg")},
    )
    assert response.status_code == 200 and response.json()["decision"] == "pass"

    verification = db_session.scalar(select(Verification).where(Verification.reference == body["reference"]))
    step = db_session.scalar(select(Step).where(Step.verification_id == verification.id,
                                                Step.step == "document"))
    assert step.status == StepStatus.passed
    assert step.extracted["document_number"] == "1199980012345678"

    kinds = {f.kind for f in db_session.scalars(
        select(VerificationFile).where(VerificationFile.verification_id == verification.id))}
    assert kinds == {FileKind.document_front, FileKind.document_back}


def test_what_the_document_proves_moves_onto_the_person(client, api_key, db_session, monkeypatch):
    stub_document(monkeypatch)
    body = open_verification(client, api_key)
    client.post(f"/v1/sessions/{token_of(body)}/document", data={"document_type": "id_card"},
                files={"front": ("front.jpg", image_bytes(), "image/jpeg")})
    verification = db_session.scalar(select(Verification).where(Verification.reference == body["reference"]))
    db_session.refresh(verification.applicant)
    assert verification.applicant.last_name == "MUGISHA"
    assert verification.applicant.country == "RWA"


def test_a_failed_document_fails_the_verification(client, api_key, db_session, monkeypatch):
    stub_document(monkeypatch, decision=Decision.failed, reasons=["DOB_MISMATCH"])
    body = open_verification(client, api_key)
    client.post(f"/v1/sessions/{token_of(body)}/document", data={"document_type": "id_card"},
                files={"front": ("front.jpg", image_bytes(), "image/jpeg")})
    after = client.get(f"/v1/verifications/{body['reference']}", headers=api_key).json()
    assert after["status"] == "failed" and after["event"] == "verification.declined"
    assert "DOB_MISMATCH" in after["reasons"]


def test_a_retry_is_a_new_attempt_not_an_overwrite(client, api_key, db_session, monkeypatch):
    stub_document(monkeypatch, decision=Decision.failed, reasons=["IMAGE_BLURRY"])
    body = open_verification(client, api_key)
    client.post(f"/v1/sessions/{token_of(body)}/document", data={"document_type": "id_card"},
                files={"front": ("front.jpg", image_bytes(), "image/jpeg")})
    stub_document(monkeypatch)
    client.post(f"/v1/sessions/{token_of(body)}/document", data={"document_type": "id_card"},
                files={"front": ("front.jpg", image_bytes(), "image/jpeg")})

    verification = db_session.scalar(select(Verification).where(Verification.reference == body["reference"]))
    attempts = db_session.scalars(
        select(Step).where(Step.verification_id == verification.id, Step.step == "document")
        .order_by(Step.attempt)).all()
    assert [a.attempt for a in attempts] == [1, 2]
    assert [a.status for a in attempts] == [StepStatus.failed, StepStatus.passed]


def test_steps_have_to_be_done_in_order(client, api_key):
    body = open_verification(client, api_key)
    response = client.post(f"/v1/sessions/{token_of(body)}/face",
                           files=[("frames", (f"f{i}.jpg", image_bytes(), "image/jpeg")) for i in range(6)])
    assert response.status_code == 409
    assert "document" in response.json()["detail"]


def test_a_step_the_session_did_not_ask_for_is_refused(client, api_key, monkeypatch):
    stub_document(monkeypatch)
    body = open_verification(client, api_key, services=["document"])
    client.post(f"/v1/sessions/{token_of(body)}/document", data={"document_type": "id_card"},
                files={"front": ("front.jpg", image_bytes(), "image/jpeg")})
    response = client.post(f"/v1/sessions/{token_of(body)}/face",
                           files=[("frames", (f"f{i}.jpg", image_bytes(), "image/jpeg")) for i in range(6)])
    assert response.status_code == 422


def test_the_person_cannot_change_who_is_being_checked(client, api_key, db_session, monkeypatch):
    """The document is checked against what the client claimed at initiation,
    not against anything sent with the images."""
    seen = {}
    from app.api.v1.routes import session_steps

    def fake_scan(document_type, front, back=None, applicant=None, today=None, require_adult=True):
        seen["applicant"] = applicant
        return DocumentCheckResponse(decision=Decision.passed, reasons=[], warnings=[],
                                     document_type=document_type, extracted=None, checks=[], processing_ms=1)

    monkeypatch.setattr(session_steps, "scan_document", fake_scan)
    monkeypatch.setattr(session_steps, "_keep_document_portrait", lambda *a: "ok")
    body = open_verification(client, api_key)
    client.post(f"/v1/sessions/{token_of(body)}/document",
                data={"document_type": "id_card", "first_name": "Someone", "last_name": "Else"},
                files={"front": ("front.jpg", image_bytes(), "image/jpeg")})
    assert seen["applicant"].first_name == "Jean"


# Expiry

def test_an_expired_link_stops_the_session(client, api_key, db_session, monkeypatch):
    stub_document(monkeypatch)
    body = open_verification(client, api_key)
    verification = db_session.scalar(select(Verification).where(Verification.reference == body["reference"]))
    verification.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db_session.commit()

    assert client.get(f"/v1/sessions/{token_of(body)}").json()["expired"] is True
    refused = client.post(f"/v1/sessions/{token_of(body)}/document", data={"document_type": "id_card"},
                          files={"front": ("front.jpg", image_bytes(), "image/jpeg")})
    assert refused.status_code == 410

    after = client.get(f"/v1/verifications/{body['reference']}", headers=api_key).json()
    assert after["status"] == "expired" and after["event"] == "request.timeout"


def test_a_finished_verification_is_not_expired_afterwards(client, api_key, db_session, monkeypatch):
    stub_document(monkeypatch)
    body = open_verification(client, api_key, services=["document"])
    client.post(f"/v1/sessions/{token_of(body)}/document", data={"document_type": "id_card"},
                files={"front": ("front.jpg", image_bytes(), "image/jpeg")})
    verification = db_session.scalar(select(Verification).where(Verification.reference == body["reference"]))
    verification.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db_session.commit()
    after = client.get(f"/v1/verifications/{body['reference']}", headers=api_key).json()
    # A verification's status is a lifecycle state ("passed"), a step's is a
    # verdict ("pass"), which is the same vocabulary the document checks use.
    assert after["status"] == "passed"


# Reading it back

def test_one_client_cannot_read_anothers_verification(client, api_key, db_session):
    body = open_verification(client, api_key)
    other = client.post("/v1/auth/register", json={**ACCOUNT, "email": "other@tuesday.africa"}).json()
    bearer = {"Authorization": f"Bearer {other['access_token']}"}
    their_key = {"X-API-Key": client.post("/v1/api-keys", json={"name": "theirs"}, headers=bearer).json()["key"]}
    assert client.get(f"/v1/verifications/{body['reference']}", headers=their_key).status_code == 404


def test_the_document_page_asks_only_what_is_left_to_ask(client, api_key):
    """In a session the person's details came from the client at initiation, so
    the page asks which document they are holding and nothing else."""
    body = open_verification(client, api_key)
    page = client.get(f"/verify/{token_of(body)}/document").text
    assert "asked-here" in page                       # the identity rows are marked
    assert "Your details were given when this check was started" in page
    assert 'id="document-type"' in page
    # Marking them is not enough: .row sets display: grid, which beats the
    # hidden attribute, so the rule that actually hides them has to be there.
    assert ".row[hidden] { display: none; }" in page


def test_the_session_pages_are_served(client, api_key):
    body = open_verification(client, api_key)
    token = token_of(body)
    assert body["session_url"].endswith(f"/verify/{token}")
    for path in (f"/verify/{token}", f"/verify/{token}/document", f"/verify/{token}/face"):
        assert client.get(path).status_code == 200


def test_one_submission_is_one_attempt(client, api_key, db_session, monkeypatch):
    """The capture page used to read the front on its own to check it was
    legible, then send it again with the back, so one photographed document was
    recorded as two attempts."""
    stub_document(monkeypatch)
    body = open_verification(client, api_key)
    client.post(f"/verify/{token_of(body)}".replace("/verify/", "/v1/sessions/") + "/document",
                data={"document_type": "id_card"},
                files={"front": ("front.jpg", image_bytes(), "image/jpeg")})

    verification = db_session.scalar(select(Verification).where(Verification.reference == body["reference"]))
    attempts = db_session.scalars(
        select(Step).where(Step.verification_id == verification.id, Step.step == "document")).all()
    assert [a.attempt for a in attempts] == [1]


def test_the_face_page_can_show_the_document_portrait(client, api_key, db_session, monkeypatch, tmp_path):
    """An empty panel captioned "Document on file" told the person nothing. The
    portrait cut from their document is served to the page instead."""
    from app.api.v1.routes import session_steps
    from app.db.models import FileKind, VerificationFile
    from app.storage import evidence

    stub_document(monkeypatch)

    def keep_portrait(db, verification, step, front):
        stored = evidence.keep(verification.id, b"portrait-bytes", "document_portrait")
        db.add(VerificationFile(verification_id=verification.id, step_id=step.id,
                                kind=FileKind.document_portrait, storage_key=stored.storage_key,
                                content_type=stored.content_type, byte_size=stored.byte_size,
                                sha256=stored.sha256))
        return "ok"

    monkeypatch.setattr(session_steps, "_keep_document_portrait", keep_portrait)
    body = open_verification(client, api_key)
    token = token_of(body)
    assert client.get(f"/v1/sessions/{token}/document-portrait").status_code == 404   # nothing read yet

    client.post(f"/v1/sessions/{token}/document", data={"document_type": "id_card"},
                files={"front": ("front.jpg", image_bytes(), "image/jpeg")})
    served = client.get(f"/v1/sessions/{token}/document-portrait")
    assert served.status_code == 200 and served.content == b"portrait-bytes"


def test_the_live_guidance_works_on_a_session_token(client, api_key, monkeypatch):
    """The face page took its id from the last path segment, which in a session
    is the word "face", so no resting pose was ever learned and the prompt could
    never advance."""
    from app.storage import sessions

    body = open_verification(client, api_key)
    token = token_of(body)
    sessions.remember_resting(token, object())
    assert sessions.resting_for(token) is not None
    assert sessions.resting_for("face") is None


def test_the_portrait_is_decided_by_the_detector_that_has_to_match_it(client, api_key, db_session, monkeypatch):
    """Two detectors run on a document: YuNet for the picture people see, SCRFD
    for the match. Asking YuNet whether there is a portrait at all meant a card
    it could not see a face in was recorded as having none, no template was
    written, and the face step later failed with NO_FACE_ON_DOCUMENT on a
    document that plainly had a face."""
    from app.api.v1.routes import session_steps
    from app.db.models import FaceTemplate

    stub_document(monkeypatch, stub_portrait=False)     # the real portrait handler runs
    # YuNet sees nothing; the matcher's detector sees a face.
    monkeypatch.setattr(session_steps, "detect_faces", lambda image: [])
    monkeypatch.setattr(session_steps.recognition, "find_face",
                        lambda image: (np.array([10.0, 10.0, 210.0, 260.0, 0.9]),
                                       np.array([[60.0, 90.0], [150.0, 90.0], [105.0, 140.0],
                                                 [70.0, 190.0], [140.0, 190.0]])))
    monkeypatch.setattr(session_steps, "embed", lambda image: np.full(512, 0.1, dtype=np.float32))

    body = open_verification(client, api_key)
    response = client.post(f"/v1/sessions/{token_of(body)}/document", data={"document_type": "id_card"},
                           files={"front": ("front.jpg", image_bytes(), "image/jpeg")})
    assert response.json()["portrait"] == "ok"

    verification = db_session.scalar(select(Verification).where(Verification.reference == body["reference"]))
    kinds = [t.source for t in db_session.scalars(
        select(FaceTemplate).where(FaceTemplate.verification_id == verification.id))]
    assert "document" in kinds


def test_a_document_with_no_face_stops_there(client, api_key, db_session, monkeypatch):
    """An identity document has a face on it. Without one, the step does not
    pass, so nobody walks on to a face check that has nothing to compare
    against and fails for a reason they cannot act on."""
    from app.api.v1.routes import session_steps

    stub_document(monkeypatch)
    monkeypatch.setattr(session_steps, "_keep_document_portrait", lambda *a: "missing")
    body = open_verification(client, api_key)
    token = token_of(body)
    response = client.post(f"/v1/sessions/{token}/document", data={"document_type": "id_card"},
                           files={"front": ("front.jpg", image_bytes(), "image/jpeg")}).json()
    assert response["decision"] == "retake"
    assert "NO_PORTRAIT_ON_DOCUMENT" in response["reasons"]

    # And the face step is not reachable.
    blocked = client.post(f"/v1/sessions/{token}/face",
                          files=[("frames", (f"f{i}.jpg", image_bytes(), "image/jpeg")) for i in range(6)])
    assert blocked.status_code == 409


def test_a_birth_certificate_may_have_no_face(client, api_key, monkeypatch):
    """The exception: a certificate carries no photograph, and never did."""
    from app.api.v1.routes import session_steps

    stub_document(monkeypatch)
    monkeypatch.setattr(session_steps, "_keep_document_portrait", lambda *a: "missing")
    body = open_verification(client, api_key, services=["document"])
    response = client.post(f"/v1/sessions/{token_of(body)}/document",
                           data={"document_type": "birth_certificate"},
                           files={"front": ("front.jpg", image_bytes(), "image/jpeg")}).json()
    assert response["decision"] == "pass"
    assert "NO_PORTRAIT_ON_DOCUMENT" not in response["reasons"]


# What the face step records about a generated image


def stub_face(monkeypatch, ai_check: CheckResult):
    """A face step whose liveness and match both pass, carrying `ai_check`."""
    from app.api.v1.routes import session_steps
    from app.schemas.face import ChallengeProgress, FaceMatchResult, LivenessResponse

    def fake_liveness(payloads, challenge_type=None, resting=None, claims=None):
        return LivenessResponse(
            decision=Decision.passed, reasons=[], warnings=list(ai_check.warnings),
            challenge=ChallengeProgress(type="circle", required=["left", "right"],
                                        completed=["left", "right"], missing=[]),
            baseline_yaw=0.0, baseline_pitch=0.0, planarity=12.0,
            frames_received=6, frames_with_face=6, poses=[], capture=None,
            checks=[CheckResult(name="capture", status=CheckStatus.passed), ai_check],
            processing_ms=5,
        )

    def fake_match(document_vector, captured):
        return FaceMatchResult(score=0.6, best_score=0.6, frames_compared=1, threshold=0.4,
                               check=CheckResult(name="face_match", status=CheckStatus.passed))

    monkeypatch.setattr(session_steps, "check_liveness", fake_liveness)
    monkeypatch.setattr(session_steps, "match_faces", fake_match)


def face_step_row(client, api_key, db_session, monkeypatch, ai_check):
    stub_document(monkeypatch)
    stub_face(monkeypatch, ai_check)
    body = open_verification(client, api_key)
    token = token_of(body)
    client.post(f"/v1/sessions/{token}/document", data={"document_type": "id_card"},
                files={"front": ("front.jpg", image_bytes(), "image/jpeg")})
    response = client.post(f"/v1/sessions/{token}/face",
                           files=[("frames", (f"f{i}.jpg", image_bytes(), "image/jpeg")) for i in range(6)])
    assert response.status_code == 200, response.text
    verification = db_session.scalar(select(Verification).where(Verification.reference == body["reference"]))
    step = db_session.scalar(select(Step).where(Step.verification_id == verification.id, Step.step == "face"))
    check = db_session.scalar(select(FaceCheck).where(FaceCheck.verification_id == verification.id))
    return step, check


def test_a_generated_selfie_is_flagged_on_the_face_step_itself(client, api_key, db_session, monkeypatch):
    """The face step used to hand finish_step a result with no checks in it, so
    verification_steps.ai_generated was null on every selfie even when the frames had
    been scored and face_checks said so. Production had a selfie scored 0.002 with
    nothing on its step."""
    flagged = CheckResult(name="ai_generated", status=CheckStatus.passed,
                          warnings=["AI_GENERATED_IMAGE"], details={"score": 0.99})
    step, check = face_step_row(client, api_key, db_session, monkeypatch, flagged)
    assert step.status == StepStatus.passed          # a warning, never a failure
    assert step.ai_generated is True
    assert check.deepfake_checked is True and check.deepfake_passed is False
    assert check.deepfake_score == 0.99


def test_a_real_selfie_is_recorded_as_checked_and_clean(client, api_key, db_session, monkeypatch):
    clean = CheckResult(name="ai_generated", status=CheckStatus.passed, details={"score": 0.002})
    step, check = face_step_row(client, api_key, db_session, monkeypatch, clean)
    assert step.ai_generated is False
    assert check.deepfake_passed is True and check.deepfake_score == 0.002


def test_a_selfie_that_could_not_be_scored_says_nothing_either_way(client, api_key, db_session, monkeypatch):
    skipped = CheckResult(name="ai_generated", status=CheckStatus.skipped,
                          details={"reason": "no image large enough to score"})
    step, check = face_step_row(client, api_key, db_session, monkeypatch, skipped)
    assert step.ai_generated is None
    assert check.deepfake_checked is False
