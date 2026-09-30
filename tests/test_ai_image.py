from datetime import date

import numpy as np
import pytest

from app.core.config import settings
from app.engines import ai_image as engine
from app.pipeline import ai_image as check_module
from app.pipeline.ai_image import MIN_SIDE, check_ai_generated
from app.pipeline.document import scan_document
from app.pipeline.liveness import _ai_frames
from app.schemas.document import Applicant, DocumentType
from app.schemas.face import CapturedFace
from tests.synthetic import mono_font, render_document, to_bytes
from tests.test_documents import RWA_PASSPORT, TODAY

needs_font = pytest.mark.skipif(mono_font(20) is None, reason="no monospace font to render test documents")


def image(side: int = MIN_SIDE, seed: int = 0) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, 255, (side, side, 3), dtype=np.uint8)


@pytest.fixture
def scores(monkeypatch):
    """Stand the detector in with fixed probabilities, one per image, in order."""
    queue = {"values": []}

    def fake(images):
        return queue["values"][: len(images)]

    monkeypatch.setattr(check_module.ai_image, "available", lambda: True)
    monkeypatch.setattr(check_module.ai_image, "probabilities", fake)
    return queue


# The check on its own


def test_skipped_when_the_model_is_not_installed(monkeypatch):
    monkeypatch.setattr(check_module.ai_image, "available", lambda: False)
    result = check_ai_generated([("front", image())], subject="document")
    assert result.status == "skipped"
    assert result.details["reason"] == "model not installed"
    assert result.reasons == [] and result.warnings == []


def test_images_too_small_to_score_are_skipped(scores):
    scores["values"] = [0.99]
    result = check_ai_generated([("portrait", image(MIN_SIDE - 1))], subject="document")
    assert result.status == "skipped"
    assert result.details["too_small"] == ["portrait"]


def test_a_detector_error_skips_rather_than_failing_the_person(monkeypatch):
    def broken(_):
        raise RuntimeError("onnxruntime fell over")

    monkeypatch.setattr(check_module.ai_image, "available", lambda: True)
    monkeypatch.setattr(check_module.ai_image, "probabilities", broken)
    result = check_ai_generated([("front", image())], subject="document")
    assert result.status == "skipped"
    assert result.details["reason"] == "detector error"


def test_real_document_passes(scores):
    scores["values"] = [0.01, 0.03]
    result = check_ai_generated([("front", image()), ("back", image(seed=1))], subject="document")
    assert result.status == "pass"
    assert result.details["scores"] == {"front": 0.01, "back": 0.03}


def test_document_takes_the_highest_score_so_one_generated_side_is_enough(scores):
    scores["values"] = [0.02, 0.97]
    result = check_ai_generated([("front", image()), ("portrait", image(seed=1))], subject="document")
    assert result.status == "fail"
    assert result.reasons == ["AI_GENERATED_DOCUMENT"]
    assert result.details["score"] == 0.97 and result.details["combined"] == "max"


def test_middling_document_goes_to_review(scores):
    scores["values"] = [0.6]
    result = check_ai_generated([("front", image())], subject="document")
    assert result.status == "review"
    assert result.reasons == ["AI_GENERATED_SUSPECTED"]


def test_face_takes_the_median_so_one_odd_frame_is_not_a_verdict(scores):
    scores["values"] = [0.01, 0.99, 0.02]
    result = check_ai_generated([("frame_0", image()), ("frame_1", image(seed=1)), ("frame_2", image(seed=2))], subject="face")
    assert result.status == "pass"
    assert result.details["combined"] == "median" and result.details["score"] == 0.02


def test_generated_face_fails(scores):
    scores["values"] = [0.98, 0.99, 0.95]
    result = check_ai_generated([("frame_0", image()), ("frame_1", image(seed=1)), ("frame_2", image(seed=2))], subject="face")
    assert result.status == "fail"
    assert result.reasons == ["AI_GENERATED_FACE"]


def test_middling_face_passes_with_a_warning(scores):
    scores["values"] = [0.7]
    result = check_ai_generated([("frame_0", image())], subject="face")
    assert result.status == "pass"
    assert result.warnings == ["AI_GENERATED_SUSPECTED"]


def test_thresholds_are_settings(scores, monkeypatch):
    monkeypatch.setattr(settings, "AI_IMAGE_REJECT_THRESHOLD", 0.99)
    scores["values"] = [0.95]
    assert check_ai_generated([("front", image())], subject="document").status == "review"


# Which images the steps hand over


def test_liveness_scores_the_still_and_two_frames_through_the_capture():
    images = [image(seed=i) for i in range(9)]
    still = CapturedFace(frame_index=4, blur_score=1.0, width=10, height=10, image_base64="")
    picked = [label for label, _ in _ai_frames(images, list(range(9)), still)]
    assert picked == ["frame_4", "frame_3", "frame_6"]


@needs_font
def test_a_generated_document_fails_the_document_step(scores):
    scores["values"] = [0.99, 0.99]
    result = scan_document(
        DocumentType.passport,
        to_bytes(render_document(RWA_PASSPORT)),
        applicant=Applicant(first_name="Jean", last_name="Mugisha", date_of_birth=date(1995, 3, 15), issuing_country="RWA"),
        today=TODAY,
    )
    assert result.decision == "fail"
    assert "AI_GENERATED_DOCUMENT" in result.reasons


# The engine


def test_preprocessing_matches_training():
    # A wide image: shortest edge to 440, centre 384x384, CLIP normalisation.
    wide = np.full((300, 600, 3), 255, dtype=np.uint8)
    tensor = engine._prepare(wide)
    assert tensor.shape == (3, 384, 384)
    white = (1.0 - engine._MEAN) / engine._STD
    assert np.allclose(tensor[:, 192, 192], white, atol=1e-4)


@pytest.mark.skipif(not engine.available(), reason="AI image model not installed")
def test_the_installed_model_returns_probabilities():
    probabilities = engine.probabilities([image(), image(seed=1)])
    assert len(probabilities) == 2
    assert all(0.0 <= p <= 1.0 for p in probabilities)
