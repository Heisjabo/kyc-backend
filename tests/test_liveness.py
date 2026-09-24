import io
import os

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.engines.face import Face, Landmarks
from app.engines.mesh import head_rotation, planarity
from app.engines.signals import direction_of, read_capture
from app.main import app
from app.pipeline import frame_probe as probe_module
from app.pipeline import liveness as pipeline
from app.pipeline.liveness import check_liveness
from app.rules import face as rules
from tests.meshes import mesh

client = TestClient(app)


def frame_bytes(width: int = 640, height: int = 640, seed: int = 0) -> bytes:
    rng = np.random.default_rng(seed)
    noise = rng.integers(60, 200, (height, width, 3), dtype=np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(noise).save(buffer, format="JPEG")
    return buffer.getvalue()


def face_at(x: int = 220, y: int = 200, size: int = 220, count: int = 1):
    marks = Landmarks(
        right_eye=(x + size * 0.3, y + size * 0.4),
        left_eye=(x + size * 0.7, y + size * 0.4),
        nose=(x + size * 0.5, y + size * 0.6),
        right_mouth=(x + size * 0.35, y + size * 0.9),
        left_mouth=(x + size * 0.65, y + size * 0.9),
    )
    return [Face(x=x, y=y, width=size, height=size, score=0.99, landmarks=marks)] * count


def stub(monkeypatch, faces, meshes):
    """One list of faces and one mesh per frame, in order."""
    face_calls, mesh_calls = iter(faces), iter(meshes)
    monkeypatch.setattr(pipeline, "detect_faces", lambda image: next(face_calls))
    monkeypatch.setattr(pipeline, "face_mesh", lambda image, face: next(mesh_calls))


def run(monkeypatch, meshes, faces=None, challenge="full"):
    faces = faces or [face_at() for _ in meshes]
    stub(monkeypatch, faces, meshes)
    return check_liveness([frame_bytes(seed=i) for i in range(len(meshes))], challenge_type=challenge)


# A capture that does everything asked: nod up and down, turn both ways.
def full_capture():
    return [
        mesh(), mesh(),
        mesh(), mesh(0, -20), mesh(0, -20),              # up
        mesh(), mesh(0, 20), mesh(0, 20),                # down
        mesh(-30), mesh(-30),                            # left
        mesh(30), mesh(30),                              # right
        mesh(), mesh(),
    ]


# Pose and signals

@pytest.mark.parametrize(
    "yaw,pitch,expected",
    [(0, 0, "centre"), (-30, 0, "left"), (30, 0, "right"), (0, -20, "up"), (0, 20, "down"), (10, 5, "centre")],
)
def test_direction_from_measured_angles(yaw, pitch, expected):
    assert direction_of(yaw, pitch) == expected


def test_pose_is_recovered_from_the_mesh():
    for yaw in (-40, -20, 0, 25, 45):
        measured, _, _ = head_rotation(mesh(), mesh(yaw))
        assert measured == pytest.approx(yaw, abs=1.0)


def test_planarity_separates_a_face_from_a_photograph():
    """A printed photo tilted at the camera moves by a flat-surface transform.
    Measured on real captures: a face turning scores 2.3 to 3.3, a tilted photo
    0.23 to 0.36, against a line of 1.2."""
    face = planarity(mesh(), mesh(30))
    photo = planarity(mesh(flat=True), mesh(30, flat=True))
    assert face > rules.PLANARITY_MIN > photo


def test_a_held_movement_counts_but_a_glimpse_does_not():
    held = read_capture([mesh(), mesh(-30), mesh(-30), mesh()], mesh())
    glimpse = read_capture([mesh(), mesh(-30), mesh(), mesh()], mesh())
    assert "left" in held.steps_seen
    assert "left" not in glimpse.steps_seen


# The capture as a whole

def test_a_complete_capture_passes(monkeypatch):
    result = run(monkeypatch, full_capture())
    assert result.decision == "pass", result.reasons
    assert result.challenge.missing == []
    assert result.planarity > rules.PLANARITY_MIN
    assert result.capture is not None


def test_a_printed_photo_is_rejected_even_when_it_fakes_the_turns(monkeypatch):
    """The attack that beats a head-turn check on its own: a photo tilted at the
    camera. The landmarks move, but the surface is flat."""
    photo = [mesh(yaw, flat=True) for yaw in (0, 0, -30, -30, 0, 30, 30, 0)]
    result = run(monkeypatch, photo, challenge="turns")
    assert result.decision == "retake"
    assert "FLAT_SURFACE_DETECTED" in result.reasons
    assert result.challenge.missing == []          # it did complete the turns
    assert result.planarity < rules.PLANARITY_MIN


def test_a_still_photo_is_rejected(monkeypatch):
    result = run(monkeypatch, [mesh() for _ in range(8)], challenge="turns")
    assert result.decision == "retake"
    assert "CHALLENGE_NOT_COMPLETED" in result.reasons


def test_the_challenge_is_head_movement_only(monkeypatch):
    """No blink step: at browser frame rates a blink falls between frames as
    often as not, so it is not asked for."""
    assert all("blink" not in steps for steps in rules.CHALLENGES.values())


def test_missing_a_nod_fails(monkeypatch):
    frames = [mesh(), mesh(), mesh(), mesh(-30), mesh(-30), mesh(30), mesh(30), mesh()]
    result = run(monkeypatch, frames)
    assert result.decision == "retake"
    assert set(result.challenge.missing) == {"up", "down"}


def test_second_person_in_shot_is_rejected(monkeypatch):
    meshes = full_capture()
    faces = [face_at() for _ in meshes]
    faces[2] = face_at(count=2)
    result = run(monkeypatch, meshes, faces=faces)
    assert result.decision == "retake"
    assert "MULTIPLE_FACES_DETECTED" in result.reasons


def test_no_face_is_rejected(monkeypatch):
    meshes = [None] * 6
    result = run(monkeypatch, meshes, faces=[[] for _ in range(6)])
    assert result.decision == "retake"
    assert "NO_FACE_DETECTED" in result.reasons


def test_face_jumping_between_frames_goes_to_review(monkeypatch):
    meshes = full_capture()
    faces = [face_at(x=40 if i % 2 else 430, y=40 if i % 2 else 380) for i in range(len(meshes))]
    result = run(monkeypatch, meshes, faces=faces)
    assert "FACE_UNSTABLE_BETWEEN_FRAMES" in result.reasons


# Live guidance

def test_probe_reports_no_face(monkeypatch):
    monkeypatch.setattr(probe_module, "detect_faces", lambda image: [])
    answer, mesh_out = probe_module.probe_frame(frame_bytes())
    assert not answer.usable and answer.hints == ["NO_FACE"] and mesh_out is None


def test_probe_flags_a_dark_frame(monkeypatch):
    monkeypatch.setattr(probe_module, "detect_faces", lambda image: face_at())
    monkeypatch.setattr(probe_module, "face_mesh", lambda image, face: mesh())
    dark = np.full((640, 640, 3), 12, dtype=np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(dark).save(buffer, format="JPEG")
    answer, _ = probe_module.probe_frame(buffer.getvalue())
    assert "TOO_DARK" in answer.hints and not answer.usable


def test_probe_reports_direction_against_the_resting_mesh(monkeypatch):
    monkeypatch.setattr(probe_module, "detect_faces", lambda image: face_at())
    monkeypatch.setattr(probe_module, "face_mesh", lambda image, face: mesh(-30))
    answer, _ = probe_module.probe_frame(frame_bytes(), mesh())
    assert answer.direction == "left"


def test_probe_endpoint_rejects_non_images():
    response = client.post("/v1/face/frame", files={"frame": ("f.pdf", b"%PDF", "application/pdf")})
    assert response.status_code == 415


# Optional: run against real captured frames.
#   KYC_TEST_FRAMES=/path/to/folder pytest tests/test_liveness.py -k real_frames -s
@pytest.mark.skipif(not os.getenv("KYC_TEST_FRAMES"), reason="set KYC_TEST_FRAMES to a folder of captured frames")
def test_real_frames(capsys):
    import glob

    paths = sorted(glob.glob(os.path.join(os.environ["KYC_TEST_FRAMES"], "*")))
    result = check_liveness([open(path, "rb").read() for path in paths])
    with capsys.disabled():
        print(f"\n{len(paths)} frames -> {result.decision} {result.reasons} {result.warnings}")
        print(f"steps {result.challenge.completed}, missing {result.challenge.missing}, planarity {result.planarity}")
        for pose in result.poses:
            print(f"  frame {pose.index}: yaw={pose.yaw} pitch={pose.pitch} ear={pose.eye_aspect_ratio} {pose.direction}")
    assert result.frames_received == len(paths)


def page_shaped_capture():
    """What the page actually submits: a few calibration frames, then two frames
    per step (the one the guidance accepted and the one straight after), then a
    couple facing the camera. Any rule the server applies has to be satisfiable
    by this, or people are told they completed a movement and then refused."""
    return (
        [mesh(), mesh(), mesh(), mesh()]                 # calibration
        + [mesh(0, -20), mesh(0, -20)]                   # up
        + [mesh(0, 20), mesh(0, 20)]                     # down
        + [mesh(-30), mesh(-30)]                         # left
        + [mesh(30), mesh(30)]                           # right
        + [mesh(), mesh(), mesh()]                       # the still to keep
    )


def test_a_capture_shaped_like_the_page_sends_passes(monkeypatch):
    result = run(monkeypatch, page_shaped_capture())
    assert result.decision == "pass", (result.reasons, result.challenge.missing)
    assert result.challenge.missing == []


def test_the_resting_pose_from_the_guidance_is_the_one_used(monkeypatch):
    """The live guidance measures movement against the mesh captured while the
    camera was checked. If the final check picks a different reference, it can
    refuse movements the person was just told they had made."""
    # The person rests facing the camera, but the capture happens to start while
    # they are already turning, so the first submitted frame is not their resting
    # pose. Anything measured against that frame is off by the amount they had
    # already turned.
    resting = mesh()
    frames = [mesh(25), mesh(25), mesh(-25), mesh(-25), mesh(25), mesh(25), mesh(), mesh()]

    stub(monkeypatch, [face_at() for _ in frames], frames)
    against_guidance = check_liveness(
        [frame_bytes(seed=i) for i in range(len(frames))], challenge_type="turns", resting=resting
    )
    assert against_guidance.challenge.missing == []

    stub(monkeypatch, [face_at() for _ in frames], frames)
    without_it = check_liveness([frame_bytes(seed=i) for i in range(len(frames))], challenge_type="turns")
    assert without_it.challenge.missing != []      # the reference matters, so it is passed in


def test_a_claimed_frame_is_enough_when_the_frame_shows_the_movement(monkeypatch):
    """The guidance says which frame it accepted for each step. One frame per
    step is then enough, because the server measures that frame itself. Waiting
    for the movement to still be held on the next frame refused people who had
    done exactly what was asked and started to return."""
    frames = [mesh(), mesh(), mesh(-30), mesh(), mesh(30), mesh()]
    claims = {2: "left", 4: "right"}
    stub(monkeypatch, [face_at() for _ in frames], frames)
    result = check_liveness(
        [frame_bytes(seed=i) for i in range(len(frames))], challenge_type="turns", claims=claims
    )
    assert result.challenge.missing == []


def test_a_claim_the_frame_does_not_support_is_refused(monkeypatch):
    """A claim only says where to look. If that frame does not show the movement,
    the step is not counted."""
    frames = [mesh(), mesh(), mesh(), mesh(), mesh(30), mesh()]
    claims = {2: "left", 4: "right"}      # frame 2 is facing forward, not turned
    stub(monkeypatch, [face_at() for _ in frames], frames)
    result = check_liveness(
        [frame_bytes(seed=i) for i in range(len(frames))], challenge_type="turns", claims=claims
    )
    assert result.challenge.missing == ["left"]


def test_turning_left_is_the_persons_own_left():
    """Measured on a real capture: the subject's left cheek disappears from view
    as they turn to their own left, and that is the frame our yaw calls negative.
    The prompt names their side, so the two have to agree."""
    assert direction_of(-30, 0) == "left"
    assert direction_of(30, 0) == "right"
