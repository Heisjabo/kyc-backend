"""A file that says an AI tool made it: content credentials (C2PA) and IPTC source types."""

import io
from datetime import date

import pytest
from PIL import Image

from app.core.config import settings
from app.engines import provenance
from app.pipeline.ai_image import check_content_credentials
from app.pipeline.document import scan_document
from app.schemas.document import Applicant, CheckStatus, DocumentType
from app.services.verifications import ai_generated_flag
from tests.test_documents import TODAY, needs_font, rwa_id_front


def _jpeg(data: bytes | None = None) -> bytes:
    if data is not None:
        image = Image.open(io.BytesIO(data)).convert("RGB")
    else:
        image = Image.new("RGB", (64, 64), "white")
    out = io.BytesIO()
    image.save(out, format="JPEG")
    return out.getvalue()


def _with_credentials(jpeg: bytes, *words: bytes) -> bytes:
    """A JPEG with an APP11 JUMBF block, which is where C2PA credentials live, holding `words`."""
    payload = b"JP\x00\x01\x00\x00\x00\x01" + b"jumb\x00\x00\x00\x1ejumdc2pa\x00" + b" ".join(words)
    segment = b"\xff\xeb" + (len(payload) + 2).to_bytes(2, "big") + payload
    return jpeg[:2] + segment + jpeg[2:]


GEMINI = (b"trainedAlgorithmicMedia", b"Google")


def test_a_plain_file_has_no_credentials():
    assert provenance.content_credentials(_jpeg()) == {}


def test_credentials_that_declare_ai_are_found_with_the_tool_named():
    found = provenance.content_credentials(_with_credentials(_jpeg(), *GEMINI))
    assert found["CONTENT_CREDENTIALS_PRESENT"] == "C2PA manifest"
    assert found["AI_GENERATED_DECLARED"] == "trainedAlgorithmicMedia (Google)"


def test_credentials_that_do_not_declare_ai_are_not_a_hit():
    """A camera or editor can sign a real photo; that is not a generated image."""
    found = provenance.content_credentials(_with_credentials(_jpeg(), b"digitalCapture", b"Canon"))
    assert "AI_GENERATED_DECLARED" not in found


def test_a_real_photo_edited_with_generative_tools_is_declared_too():
    found = provenance.content_credentials(_with_credentials(_jpeg(), b"compositeWithTrainedAlgorithmicMedia"))
    assert found["AI_GENERATED_DECLARED"].startswith("compositeWithTrainedAlgorithmicMedia")


def test_the_check_sends_a_declared_file_to_review_by_default():
    check = check_content_credentials([("front", _with_credentials(_jpeg(), *GEMINI))], subject="document")
    assert check.status == CheckStatus.review
    assert check.reasons == ["AI_GENERATED_DECLARED"] and "AI_GENERATED_IMAGE" in check.warnings


def test_the_check_can_only_flag(monkeypatch):
    monkeypatch.setattr(settings, "AI_CONTENT_CREDENTIALS_ACTION", "flag")
    check = check_content_credentials([("front", _with_credentials(_jpeg(), *GEMINI))], subject="document")
    assert check.status == CheckStatus.passed and check.reasons == []
    assert "AI_GENERATED_IMAGE" in check.warnings


def test_the_check_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(settings, "AI_CONTENT_CREDENTIALS_ACTION", "off")
    check = check_content_credentials([("front", _with_credentials(_jpeg(), *GEMINI))], subject="document")
    assert check.status == CheckStatus.skipped and check.warnings == []


def test_an_undeclared_file_passes_cleanly():
    check = check_content_credentials([("front", _jpeg()), ("back", None)], subject="document")
    assert check.status == CheckStatus.passed and check.warnings == []


def test_a_declared_file_sets_the_step_flag_whatever_the_detector_said():
    declared = check_content_credentials([("front", _with_credentials(_jpeg(), *GEMINI))], subject="document")
    detector = {"name": "ai_generated", "status": "passed", "warnings": []}
    assert ai_generated_flag({"checks": [detector, declared.model_dump(mode="json")]}) is True
    assert ai_generated_flag({"checks": [detector]}) is False


@needs_font
def test_a_document_that_declares_itself_generated_goes_to_review():
    applicant = Applicant(first_name="Jean", last_name="Mugisha", date_of_birth=date(1995, 3, 15), issuing_country="RWA")
    plain = _jpeg(rwa_id_front())
    assert scan_document(DocumentType.id_card, plain, applicant=applicant, today=TODAY).decision == "pass"
    result = scan_document(DocumentType.id_card, _with_credentials(plain, *GEMINI), applicant=applicant, today=TODAY)
    assert result.decision == "review"
    assert "AI_GENERATED_DECLARED" in result.reasons
    assert "AI_GENERATED_IMAGE" in result.warnings
