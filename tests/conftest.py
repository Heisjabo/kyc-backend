import pytest

from app.core.config import settings


@pytest.fixture(autouse=True)
def _legacy_endpoints_on_for_the_legacy_tests(monkeypatch):
    """The first-generation endpoints are off by default (they take no login).

    The tests that exercise them switch them on here; tests/test_legacy_endpoints.py
    switches them off again to prove they are hidden.
    """
    monkeypatch.setattr(settings, "ENABLE_LEGACY_ENDPOINTS", True)
