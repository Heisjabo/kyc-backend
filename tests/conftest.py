import pytest

from app.core.config import settings


@pytest.fixture(autouse=True)
def _legacy_endpoints_on_for_the_legacy_tests(monkeypatch):
    """The first-generation endpoints are off by default (they take no login).

    The tests that exercise them switch them on here; tests/test_legacy_endpoints.py
    switches them off again to prove they are hidden.
    """
    monkeypatch.setattr(settings, "ENABLE_LEGACY_ENDPOINTS", True)


@pytest.fixture(autouse=True)
def _private_callbacks_allowed_unless_a_test_says_otherwise(monkeypatch):
    """The tests use made-up callback hosts that do not resolve. The tests in
    tests/test_callbacks.py switch the public-address rule back on."""
    monkeypatch.setattr(settings, "CALLBACK_ALLOW_PRIVATE_HOSTS", True)


@pytest.fixture(autouse=True)
def _address_review_off_except_where_a_test_asks(monkeypatch):
    """Sending every address proof to a person is the default. Most address tests are
    about the other checks, so they run without it; tests/test_address_review.py
    turns it on."""
    monkeypatch.setattr(settings, "ADDRESS_PROOFS_NEED_REVIEW", False)


@pytest.fixture(autouse=True)
def _rate_limits_off_except_where_a_test_asks(monkeypatch):
    """The suite signs in and submits far more often than a person would. The tests in
    tests/test_ratelimit.py switch the limits on."""
    from app.core.ratelimit import limiter

    monkeypatch.setattr(settings, "RATE_LIMITS_ENABLED", False)
    limiter.reset()
    yield
    limiter.reset()
