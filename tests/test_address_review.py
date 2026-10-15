"""Nothing here can tell a forged proof of address from a real one, so by default a
proof that would otherwise pass goes to a person."""

import pytest

from app.core.config import settings
from app.schemas.document import CheckStatus, Decision

from tests.test_address import named, scan, utility_bill


@pytest.fixture(autouse=True)
def review_on(monkeypatch):
    monkeypatch.setattr(settings, "ADDRESS_PROOFS_NEED_REVIEW", True)


def test_a_proof_that_would_pass_goes_to_review_instead():
    result = scan(utility_bill())
    assert result.decision == Decision.review
    assert "AUTHENTICITY_NOT_VERIFIED" in result.reasons
    assert named(result)["address_document_authenticity"] == CheckStatus.review


def test_a_proof_that_fails_still_fails():
    result = scan(utility_bill(place="MOI AVENUE, NAIROBI"))
    assert result.decision == Decision.failed
    assert "ADDRESS_MISMATCH" in result.reasons


def test_the_setting_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(settings, "ADDRESS_PROOFS_NEED_REVIEW", False)
    assert scan(utility_bill()).decision == Decision.passed


def test_the_default_is_on():
    assert type(settings).model_fields["ADDRESS_PROOFS_NEED_REVIEW"].default is True
