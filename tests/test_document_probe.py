"""Live guidance while a document is held up to the camera.

The frames are built here rather than photographed, so what is tested is the
decision each reading leads to, not the detector.
"""

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image
import io

from app.main import app
from app.pipeline import document_probe
from app.pipeline.document_probe import probe_document_frame
from app.rules import document as rules

client = TestClient(app)


def card(text_lines: int = 20, brightness: int = 180, width: int = 640, height: int = 400) -> np.ndarray:
    """A picture with the printed-text structure of an identity card: rows of
    words, which is what the probe counts, rather than unbroken rules."""
    image = np.full((height, width, 3), brightness, dtype=np.uint8)
    for row in range(text_lines):
        y = 16 + row * (height - 32) // max(text_lines, 1)
        x = 50
        while x < width - 120:
            word = 30 + (x + row) % 55
            cv2.rectangle(image, (x, y), (x + word, y + 12), (25, 25, 25), -1)
            x += word + 14
    return image


def jpeg(image: np.ndarray, quality: int = 92) -> bytes:
    return cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])[1].tobytes()


def with_portrait(image: np.ndarray, ratio: float = 0.34):
    """Stub a found face of the given height, since a drawn face is not a face."""
    from app.engines.face import Face, Landmarks

    height = int(image.shape[0] * ratio)
    marks = Landmarks(right_eye=(0, 0), left_eye=(0, 0), nose=(0, 0), right_mouth=(0, 0), left_mouth=(0, 0))
    return [Face(x=20, y=20, width=height, height=height, score=0.9, landmarks=marks)]


def test_a_good_capture_is_usable(monkeypatch):
    monkeypatch.setattr(document_probe, "detect_faces", lambda image: with_portrait(image))
    answer = probe_document_frame(jpeg(card()))
    assert answer.usable, answer.hints
    assert answer.hints == []


def test_a_face_alone_is_not_a_document(monkeypatch):
    """Somebody holding their face to the camera must not be captured as an ID.
    Measured on real frames: cards give 26 to 49 text lines, faces 7 to 12."""
    monkeypatch.setattr(document_probe, "detect_faces", lambda image: with_portrait(image))
    blank = np.full((400, 640, 3), 170, dtype=np.uint8)
    answer = probe_document_frame(jpeg(blank))
    assert not answer.usable
    assert "NO_DOCUMENT_IN_VIEW" in answer.hints


def test_a_card_too_far_away_is_refused(monkeypatch):
    monkeypatch.setattr(document_probe, "detect_faces", lambda image: with_portrait(image, ratio=0.1))
    answer = probe_document_frame(jpeg(card()))
    assert not answer.usable
    assert "FIT_THE_CARD_IN_THE_BOX" in answer.hints


def test_a_sideways_card_says_so_rather_than_no_portrait(monkeypatch):
    """A detector finds no face on a card lying sideways. Telling the person their
    document has no photograph would be wrong; they are told to turn it."""
    calls = {"n": 0}

    def upright_only(image):
        calls["n"] += 1
        return [] if calls["n"] == 1 else with_portrait(image)

    monkeypatch.setattr(document_probe, "detect_faces", upright_only)
    answer = probe_document_frame(jpeg(card()))
    assert "TURN_THE_CARD_UPRIGHT" in answer.hints
    assert "PORTRAIT_NOT_VISIBLE" not in answer.hints


def test_no_portrait_at_any_turn_says_the_portrait_is_missing(monkeypatch):
    monkeypatch.setattr(document_probe, "detect_faces", lambda image: [])
    answer = probe_document_frame(jpeg(card()))
    assert "PORTRAIT_NOT_VISIBLE" in answer.hints


def test_a_blurred_frame_says_hold_still(monkeypatch):
    """Measured on a real licence: everything reads at a blur score of 97, the
    country line is lost by 20. The gate sits between them."""
    monkeypatch.setattr(document_probe, "detect_faces", lambda image: with_portrait(image))
    answer = probe_document_frame(jpeg(cv2.GaussianBlur(card(), (15, 15), 0)))
    assert "HOLD_STILL" in answer.hints
    assert answer.blur_score < rules.LIVE_MIN_BLUR


def test_a_dark_frame_says_so(monkeypatch):
    monkeypatch.setattr(document_probe, "detect_faces", lambda image: with_portrait(image))
    answer = probe_document_frame(jpeg(card(brightness=40)))
    assert "TOO_DARK" in answer.hints


def test_glare_is_reported(monkeypatch):
    monkeypatch.setattr(document_probe, "detect_faces", lambda image: with_portrait(image))
    glary = card()
    cv2.rectangle(glary, (200, 100), (460, 280), (255, 255, 255), -1)
    answer = probe_document_frame(jpeg(glary))
    assert "GLARE_ON_THE_CARD" in answer.hints


def test_an_unreadable_frame_is_rejected():
    answer = probe_document_frame(b"not an image")
    assert not answer.usable
    assert answer.hints == ["FRAME_UNREADABLE"]


def test_the_endpoint_answers(monkeypatch):
    monkeypatch.setattr(document_probe, "detect_faces", lambda image: with_portrait(image))
    response = client.post("/v1/document/frame", files={"frame": ("f.jpg", jpeg(card()), "image/jpeg")})
    assert response.status_code == 200
    assert response.json()["usable"] is True


def test_the_endpoint_refuses_other_file_types():
    response = client.post("/v1/document/frame", files={"frame": ("f.pdf", b"%PDF", "application/pdf")})
    assert response.status_code == 415


def test_the_back_of_a_card_needs_no_portrait(monkeypatch):
    """A back has text but no photograph. Judged as a front it would never pass."""
    monkeypatch.setattr(document_probe, "detect_faces", lambda image: [])
    back = jpeg(card())
    assert "PORTRAIT_NOT_VISIBLE" in probe_document_frame(back, side="front").hints
    assert probe_document_frame(back, side="back").usable


def test_a_back_needs_no_particular_content(monkeypatch):
    """No MRZ, barcode or front's worth of text is asked of a back."""
    monkeypatch.setattr(document_probe, "detect_faces", lambda image: [])
    sparse = jpeg(card(text_lines=2))
    assert probe_document_frame(sparse, side="back").usable
    assert "NO_DOCUMENT_IN_VIEW" not in probe_document_frame(sparse, side="back").hints


def test_a_small_ghost_portrait_is_allowed_on_a_back(monkeypatch):
    image = card()
    monkeypatch.setattr(document_probe, "detect_faces", lambda frame: with_portrait(image, ratio=0.10))
    assert probe_document_frame(jpeg(image), side="back").usable


def test_a_front_sized_portrait_on_the_back_step_asks_for_the_back(monkeypatch):
    image = card()
    monkeypatch.setattr(document_probe, "detect_faces", lambda frame: with_portrait(image, ratio=0.34))
    assert "SHOW_THE_BACK" in probe_document_frame(jpeg(image), side="back").hints


def test_the_endpoint_takes_the_side(monkeypatch):
    monkeypatch.setattr(document_probe, "detect_faces", lambda image: [])
    response = client.post(
        "/v1/document/frame",
        data={"side": "back"},
        files={"frame": ("f.jpg", jpeg(card()), "image/jpeg")},
    )
    assert response.status_code == 200 and response.json()["usable"] is True
    bad = client.post("/v1/document/frame", data={"side": "sideways"},
                      files={"frame": ("f.jpg", jpeg(card()), "image/jpeg")})
    assert bad.status_code == 422


def test_the_capture_page_is_served():
    page = client.get("/capture")
    assert page.status_code == 200
    assert "Capture now" in page.text          # the manual button, for when guidance fails
    assert "Skip the back" in page.text        # the back is optional
    # A child is chosen by their document, not by a separate question.
    assert "Birth certificate (for children)" in page.text
