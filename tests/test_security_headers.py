"""Every response carries the headers that stop it being framed, sniffed or leaked."""

from fastapi.testclient import TestClient

from app.main import app


def test_the_api_sends_security_headers():
    client = TestClient(app)
    response = client.get("/v1/health") if client.get("/v1/health").status_code != 404 else client.get("/v1/sessions/nope")
    for name, value in {
        "x-content-type-options": "nosniff",
        "referrer-policy": "no-referrer",
        "x-frame-options": "DENY",
    }.items():
        assert response.headers[name] == value
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "camera=(self)" in response.headers["permissions-policy"]


def test_errors_carry_them_too():
    response = TestClient(app).get("/v1/sessions/made-up-token")
    assert response.status_code == 404 and response.headers["x-content-type-options"] == "nosniff"


def test_hsts_is_only_sent_over_https():
    plain = TestClient(app).get("/v1/sessions/nope")
    secure = TestClient(app, base_url="https://testserver").get("/v1/sessions/nope")
    assert "strict-transport-security" not in plain.headers
    assert secure.headers["strict-transport-security"].startswith("max-age=")
