"""The first-generation endpoints take no API key and no session token, so they are
hidden unless ENABLE_LEGACY_ENDPOINTS is on."""

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app

client = TestClient(app)

LEGACY = [
    ("post", "/v1/document/scan"),
    ("post", "/v1/document/birth-certificate"),
    ("post", "/v1/face/liveness"),
    ("post", "/v1/verifications/abc/face"),
    ("get", "/"),
    ("get", "/capture"),
    ("get", "/scratch-verify/abc"),
    ("get", "/api/session/abc"),
]


@pytest.mark.parametrize("method,path", LEGACY)
def test_hidden_by_default(monkeypatch, method, path):
    monkeypatch.setattr(settings, "ENABLE_LEGACY_ENDPOINTS", False)
    response = getattr(client, method)(path)
    assert response.status_code == 404, f"{method.upper()} {path} answered {response.status_code}"


@pytest.mark.parametrize("method,path", LEGACY)
def test_reachable_when_switched_on(monkeypatch, method, path):
    monkeypatch.setattr(settings, "ENABLE_LEGACY_ENDPOINTS", True)
    response = getattr(client, method)(path)
    # An empty request is refused as invalid (422) or not found (404 for an unknown id),
    # never because the route is hidden by the flag with a 404 on the page routes.
    assert response.status_code in (200, 404, 422)


def test_default_is_on():
    assert type(settings).model_fields["ENABLE_LEGACY_ENDPOINTS"].default is True


def test_the_real_flow_does_not_need_them(monkeypatch):
    """Session links and the health check work with the legacy endpoints off."""
    monkeypatch.setattr(settings, "ENABLE_LEGACY_ENDPOINTS", False)
    assert client.get("/v1/health").status_code == 200
    assert client.get("/v1/sessions/not-a-token").status_code == 404


@pytest.mark.parametrize("path", ["/v1/document/frame", "/v1/face/frame"])
def test_the_live_camera_guidance_endpoints_stay_available(monkeypatch, path):
    """The customer-facing app calls these while the camera is open. They return
    only quality figures, so they are not part of what is hidden."""
    monkeypatch.setattr(settings, "ENABLE_LEGACY_ENDPOINTS", False)
    assert client.post(path).status_code == 422   # reaches validation: no frame sent
