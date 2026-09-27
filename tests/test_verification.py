"""Face match and the document-then-face flow.

The models are stubbed: embeddings are plain vectors and the detector returns
faces built by hand, so these test the decision logic, not ArcFace itself.
"""

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.api.v1.routes import verifications as verifications_route
from app.main import app
from app.core.config import settings
from app.engines.recognition import pass_threshold
from app.pipeline.face_match import match_faces
from app.rules import face as face_rules
from app.storage import sessions
from tests.meshes import mesh
from tests.test_liveness import face_at, frame_bytes, full_capture, stub

client = TestClient(app)


def vector(similarity_to_reference: float) -> np.ndarray:
    """A 2-D vector whose cosine similarity to (1, 0) is the value asked for."""
    angle = np.arccos(np.clip(similarity_to_reference, -1, 1))
    return np.array([np.cos(angle), np.sin(angle)], dtype=np.float32)


REFERENCE = vector(1.0)


def rules_ttl_seconds() -> int:
    return settings.SESSION_TTL_MINUTES * 60


# The tests work relative to the configured line rather than hardcoding a number,
# so moving the line after calibration does not rewrite them.
LINE = pass_threshold()
PASSES = LINE        # at the line is a pass
FAILS = LINE - 0.15  # comfortably below it


# Face match

def test_pass_line_is_the_configured_threshold():
    assert LINE == pytest.approx(face_rules.MATCH_THRESHOLD)


def test_scores_measured_on_real_images_land_where_they_should():
    """Measured with ArcFace (buffalo_l) on one person's Rwandan ID and driving
    licence against their own capture and against six strangers: the owner scores
    0.46 to 0.58, strangers 0.02 to 0.07, against a line of 0.40."""
    owner = match_faces(REFERENCE, [vector(0.58), vector(0.55), vector(0.52)])
    stranger = match_faces(REFERENCE, [vector(0.07), vector(0.03), vector(0.02)])
    assert owner.check.status == "pass"
    assert stranger.check.status == "fail"
    assert stranger.check.reasons == ["FACE_MISMATCH"]


def test_match_passes_when_the_faces_are_alike():
    result = match_faces(REFERENCE, [vector(PASSES + 0.05), vector(PASSES + 0.02), vector(PASSES + 0.08)])
    assert result.check.status == "pass"
    assert result.threshold == pytest.approx(PASSES, abs=0.001)
    assert result.frames_compared == 3


def test_match_fails_for_a_different_person():
    result = match_faces(REFERENCE, [vector(FAILS - 0.2), vector(FAILS - 0.15), vector(FAILS - 0.22)])
    assert result.check.status == "fail"
    assert result.check.reasons == ["FACE_MISMATCH"]


def test_just_below_the_line_fails_rather_than_hedging():
    result = match_faces(REFERENCE, [vector(PASSES - 0.03), vector(PASSES - 0.05), vector(PASSES - 0.02)])
    assert result.check.status == "fail"
    assert result.check.reasons == ["FACE_MISMATCH"]


def test_one_good_frame_does_not_carry_a_poor_capture():
    # The score is the mean of the best few, so a single flattering frame among
    # poor ones cannot pass on its own.
    poor = FAILS - 0.2
    result = match_faces(REFERENCE, [vector(0.95), vector(poor), vector(poor), vector(poor)])
    assert result.best_score == pytest.approx(0.95, abs=0.01)
    assert result.check.status == "fail"


def test_a_few_grainy_frames_do_not_sink_a_genuine_capture():
    # Measured on real captures: grain pulls some frames well down while the good
    # ones stay high. The person should still pass.
    result = match_faces(
        REFERENCE,
        [vector(PASSES + 0.09), vector(PASSES + 0.06), vector(PASSES + 0.04), vector(FAILS - 0.1), vector(FAILS - 0.15)],
    )
    assert result.check.status == "pass"
    assert result.check.details["mean_all_frames"] < result.score


def test_document_without_a_portrait_is_the_documents_problem():
    result = match_faces(None, [vector(0.8)])
    assert result.check.status == "fail"
    assert result.check.reasons == ["NO_FACE_ON_DOCUMENT"]


def test_no_frontal_frame_fails():
    """Only frames where the person faced the camera reach the matcher, so an
    empty list means the capture never held one, not that no face was seen."""
    result = match_faces(REFERENCE, [])
    assert result.check.status == "fail"
    assert result.check.reasons == ["NO_FRONTAL_FRAME"]


# The flow: a scanned document, then the face step against it

def test_a_small_document_portrait_is_flagged_but_still_matched(monkeypatch):
    """A screenshot of an ID gives a portrait far smaller than a reference photo
    should be. That is worth reporting, but it does not stop the check."""
    import numpy as np

    from app.api.v1.routes import verifications as route
    from app.rules import face as rules
    from tests.test_liveness import face_at, frame_bytes

    small = face_at(size=rules.SMALL_DOCUMENT_FACE_HEIGHT - 40)
    monkeypatch.setattr(route, "detect_faces", lambda image: small)
    monkeypatch.setattr(route, "embed", lambda image, face=None: np.array([1.0, 0.0], dtype=np.float32))
    session, portrait = route.open_session("id_card", "pass", None, frame_bytes())
    assert portrait == "small"
    assert session.document_face_small
    assert session.document_embedding is not None      # still usable


@pytest.fixture
def session_with_document():
    return sessions.create(
        document_type="id_card",
        document_decision="pass",
        document_fields={"source": "visual", "full_name": "KWIZERA ALICE", "document_number": "1200280111110063"},
        document_embedding=REFERENCE,
        document_face_base64=None,
    )


def stub_capture(monkeypatch, similarity_value: float, meshes=None):
    """A complete, genuine-looking capture, matching at the similarity given."""
    meshes = meshes or full_capture()
    stub(monkeypatch, [face_at() for _ in meshes], meshes)
    monkeypatch.setattr(verifications_route, "embed", lambda image, face=None: vector(similarity_value))
    monkeypatch.setattr(verifications_route, "detect_faces", lambda image: face_at())
    monkeypatch.setattr(verifications_route, "crop_face", lambda image, face, margin=0.1: image)
    return meshes


def post_frames(verification_id: str, count: int):
    files = [("frames", (f"f{i}.jpg", frame_bytes(seed=i), "image/jpeg")) for i in range(count)]
    return client.post(f"/v1/verifications/{verification_id}/face", files=files, data={"challenge": "full"})


def test_face_step_passes_against_the_scanned_document(monkeypatch, session_with_document):
    meshes = stub_capture(monkeypatch, PASSES + 0.08)

    response = post_frames(session_with_document.id, len(meshes))
    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "pass", body["reasons"]
    assert body["match"]["score"] == pytest.approx(PASSES + 0.08, abs=0.01)
    assert body["document"]["full_name"] == "KWIZERA ALICE"


def test_face_step_fails_for_someone_else(monkeypatch, session_with_document):
    meshes = stub_capture(monkeypatch, FAILS - 0.2)

    body = post_frames(session_with_document.id, len(meshes)).json()
    assert body["decision"] == "fail"
    assert "FACE_MISMATCH" in body["reasons"]


def test_failed_liveness_is_reported_even_when_the_face_matches(monkeypatch, session_with_document):
    # A still photo of the real person: the face matches, nothing ever moves.
    meshes = stub_capture(monkeypatch, PASSES + 0.1, meshes=[mesh() for _ in range(8)])

    body = post_frames(session_with_document.id, len(meshes)).json()
    assert body["decision"] == "retake"
    assert "CHALLENGE_NOT_COMPLETED" in body["reasons"]


def test_a_flat_photo_that_completes_the_turns_is_still_rejected(monkeypatch, session_with_document):
    """The tilted-print attack: the movements happen, but the surface is flat."""
    photo = [mesh(yaw, flat=True) for yaw in (0, 0, -30, -30, 0, 30, 30, 0, 0)]
    stub_capture(monkeypatch, PASSES + 0.1, meshes=photo)

    body = post_frames(session_with_document.id, len(photo)).json()
    assert body["decision"] == "retake"
    assert "FLAT_SURFACE_DETECTED" in body["reasons"]


def test_unknown_verification_is_rejected():
    response = post_frames("does-not-exist", count=8)
    assert response.status_code == 404


def test_expired_session_is_gone(session_with_document):
    session_with_document.created_at -= rules_ttl_seconds() + 60
    assert sessions.get(session_with_document.id) is None


# Test pages

def test_pages_render():
    assert "Document scan" in client.get("/").text
    # /verify/{token} belongs to a session now; the old flow's face page moved
    # aside so the two do not answer the same URL.
    assert "Face check" in client.get("/scratch-verify/anything").text
    assert "Identity verification" in client.get("/verify/anything").text
    assert client.get("/static/style.css").status_code == 200


def test_session_endpoint_feeds_the_face_page(session_with_document):
    body = client.get(f"/api/session/{session_with_document.id}").json()
    assert body["document_type"] == "id_card"
    assert body["fields"]["full_name"] == "KWIZERA ALICE"
    assert client.get("/api/session/nope").status_code == 404


def test_no_confidence_percentage_is_reported():
    """A similarity is not a probability, and we have no calibration set to turn
    one into the other, so the response carries the score and the line only."""
    result = match_faces(REFERENCE, [vector(PASSES + 0.1), vector(PASSES + 0.1), vector(PASSES + 0.1)])
    assert not hasattr(result, "confidence")


def test_a_stranger_fails_with_the_score_reported():
    result = match_faces(REFERENCE, [vector(FAILS - 0.2), vector(FAILS - 0.2), vector(FAILS - 0.2)])
    assert result.check.status == "fail"
    assert result.check.reasons == ["FACE_MISMATCH"]
    assert result.score < LINE


# The portrait: identity is decided on one photograph, not on the frames

def test_identity_is_decided_on_the_portrait_when_one_is_sent(monkeypatch, session_with_document):
    """The frames prove liveness. The portrait proves who it was, the way the
    providers do it: one passport-style photograph against another."""
    stub_capture(monkeypatch, PASSES + 0.08)
    files = [("frames", (f"f{i}.jpg", frame_bytes(seed=i), "image/jpeg")) for i in range(14)]
    files.append(("portrait", ("portrait.jpg", frame_bytes(seed=99), "image/jpeg")))
    response = client.post(
        f"/v1/verifications/{session_with_document.id}/face", files=files, data={"challenge": "full"}
    )
    body = response.json()
    assert response.status_code == 200
    assert body["match"]["frames_compared"] == 1
    assert body["match"]["check"]["details"]["portrait_source"] == "portrait"


def test_without_a_portrait_the_best_frontal_frame_stands_in(monkeypatch, session_with_document):
    """An API caller that only has frames still gets an answer."""
    stub_capture(monkeypatch, PASSES + 0.08)
    body = post_frames(session_with_document.id, 14).json()
    assert body["match"]["check"]["details"]["portrait_source"] == "frame"


def test_the_challenge_clip_is_accepted_and_accounted_for(monkeypatch, session_with_document):
    """Storage is not built yet. The clip is still received, checked and handed
    to the storage seam, so switching storage on changes nothing else."""
    stub_capture(monkeypatch, PASSES + 0.08)
    kept = {}
    monkeypatch.setattr(
        verifications_route.evidence, "keep",
        lambda reference, data, kind, content_type="image/jpeg": kept.update(
            kind=kind, size=len(data), type=content_type
        ),
    )
    files = [("frames", (f"f{i}.jpg", frame_bytes(seed=i), "image/jpeg")) for i in range(14)]
    files.append(("video", ("challenge.webm", b"\x1a\x45\xdf\xa3fake-clip", "video/webm")))
    response = client.post(
        f"/v1/verifications/{session_with_document.id}/face", files=files, data={"challenge": "full"}
    )
    assert response.status_code == 200
    assert kept["kind"] == "challenge_video" and kept["type"] == "video/webm" and kept["size"] > 0


def test_a_clip_that_is_not_video_is_refused(monkeypatch, session_with_document):
    stub_capture(monkeypatch, PASSES + 0.08)
    files = [("frames", (f"f{i}.jpg", frame_bytes(seed=i), "image/jpeg")) for i in range(14)]
    files.append(("video", ("clip.exe", b"MZ", "application/octet-stream")))
    response = client.post(
        f"/v1/verifications/{session_with_document.id}/face", files=files, data={"challenge": "full"}
    )
    assert response.status_code == 415


