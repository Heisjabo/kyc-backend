"""Who may call this API from a browser.

The verification journey is served from its own domain and the API from another, so
without these headers a browser would send an upload, the service would do the work,
and the page would be refused sight of the answer. That failure looks like a network
blip and invites the person to submit their document a second time.
"""

import importlib

from fastapi.testclient import TestClient

from app.core.config import settings

JOURNEY = "https://pangolinkyc.tuesday.africa"


def build(monkeypatch, origins: str) -> TestClient:
    """A fresh app, since the middleware is wired when the app is created."""
    monkeypatch.setattr(settings, "CORS_ORIGINS", origins)
    import app.main

    return TestClient(importlib.reload(app.main).app)


def test_any_origin_is_allowed_by_default():
    assert settings.CORS_ORIGINS == "*"


def test_a_browser_on_another_domain_may_read_the_answer(monkeypatch):
    client = build(monkeypatch, "*")
    response = client.get("/v1/health", headers={"Origin": JOURNEY})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"


def test_a_preflight_is_answered_for_an_upload(monkeypatch):
    """What the browser asks before a POST carrying a header of its own."""
    client = build(monkeypatch, "*")
    response = client.options(
        "/v1/sessions/any-token/document",
        headers={
            "Origin": JOURNEY,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-api-key",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
    assert "POST" in response.headers["access-control-allow-methods"]


def test_credentials_are_never_allowed(monkeypatch):
    """A wildcard and credentials are mutually exclusive in every browser, and
    nothing here authenticates with a cookie."""
    client = build(monkeypatch, "*")
    response = client.get("/v1/health", headers={"Origin": JOURNEY})
    assert "access-control-allow-credentials" not in response.headers


def test_a_list_of_origins_narrows_it(monkeypatch):
    """The lever, for when this should be only our own journey."""
    client = build(monkeypatch, f"{JOURNEY}, https://kyc.example.com")
    allowed = client.get("/v1/health", headers={"Origin": JOURNEY})
    assert allowed.headers["access-control-allow-origin"] == JOURNEY

    refused = client.get("/v1/health", headers={"Origin": "https://somewhere-else.test"})
    # The request still answers; the browser is simply not told it may read it.
    assert refused.status_code == 200
    assert "access-control-allow-origin" not in refused.headers
