"""Callback and redirect URLs: what we accept, where we send data, and how it is signed."""

import hashlib
import hmac
import json

import pytest
from pydantic import ValidationError

from app.core import security, urls
from app.core.config import settings
from app.schemas.verification import CreateVerificationRequest

PUBLIC = [("93.184.216.34",)]


@pytest.fixture(autouse=True)
def strict(monkeypatch):
    monkeypatch.setattr(settings, "CALLBACK_ALLOW_PRIVATE_HOSTS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")


def resolves_to(monkeypatch, *addresses):
    monkeypatch.setattr(
        urls.socket, "getaddrinfo",
        lambda host, *a, **k: [(2, 1, 6, "", (address, 0)) for address in addresses],
    )


def make(**fields):
    return CreateVerificationRequest(services=["document"], **fields)


@pytest.mark.parametrize("bad", [
    "javascript:alert(document.domain)",
    "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
    "file:///etc/passwd",
    "http://client.example.com/hook",
    "https://user:secret@client.example.com/hook",
    "https:///no-host",
    "https://client.example.com/\nX-Injected: 1",
    "//client.example.com/hook",
    "ftp://client.example.com/",
])
def test_callback_refuses_unsafe_schemes_and_shapes(monkeypatch, bad):
    resolves_to(monkeypatch, "93.184.216.34")
    with pytest.raises(ValidationError):
        make(callback_url=bad)


@pytest.mark.parametrize("address", [
    "169.254.169.254",   # cloud metadata
    "127.0.0.1",
    "10.1.2.3",
    "172.16.0.9",
    "192.168.1.5",
    "::1",
    "fd00::1",
    "::ffff:127.0.0.1",
    "0.0.0.0",
])
def test_callback_refuses_internal_addresses(monkeypatch, address):
    resolves_to(monkeypatch, address)
    with pytest.raises(ValidationError):
        make(callback_url="https://client.example.com/kyc")


def test_callback_refuses_a_host_with_one_internal_address_among_public_ones(monkeypatch):
    resolves_to(monkeypatch, "93.184.216.34", "10.0.0.7")
    with pytest.raises(ValidationError):
        make(callback_url="https://client.example.com/kyc")


def test_callback_refuses_a_host_that_does_not_resolve(monkeypatch):
    def fail(*a, **k):
        raise OSError("no such host")
    monkeypatch.setattr(urls.socket, "getaddrinfo", fail)
    with pytest.raises(ValidationError):
        make(callback_url="https://nowhere.invalid/kyc")


def test_callback_accepts_a_public_https_address(monkeypatch):
    resolves_to(monkeypatch, "93.184.216.34")
    assert make(callback_url="https://client.example.com/kyc").callback_url == "https://client.example.com/kyc"


def test_http_is_allowed_only_in_development(monkeypatch):
    resolves_to(monkeypatch, "93.184.216.34")
    with pytest.raises(ValidationError):
        make(callback_url="http://client.example.com/kyc")
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    assert make(callback_url="http://client.example.com/kyc").callback_url


@pytest.mark.parametrize("bad", [
    "javascript:alert(document.domain)",
    "JaVaScRiPt:alert(1)",
    "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
    "vbscript:msgbox(1)",
    "http://client.example.com/done",
    "https://user:pw@client.example.com/",
    "https://client.example.com/\r\nSet-Cookie: a=b",
    "/relative/path",
])
def test_redirect_refuses_unsafe_values(bad):
    with pytest.raises(ValidationError):
        make(redirect_url=bad)


def test_redirect_accepts_https():
    assert make(redirect_url="https://client.example.com/done?ref=1").redirect_url == "https://client.example.com/done?ref=1"


def test_empty_and_missing_values_are_fine():
    assert make().callback_url is None
    assert make(callback_url="", redirect_url="").redirect_url is None


def test_signature_matches_what_a_client_computes():
    body = b'{"reference":"abc","status":"passed"}'
    header = security.sign_callback(7, body, 1760000000)
    key = security.callback_secret(7).encode()
    expected = hmac.new(key, b"1760000000." + body, hashlib.sha256).hexdigest()
    assert header == f"sha256={expected}"


def test_each_account_signs_with_its_own_key():
    assert security.callback_secret(1) != security.callback_secret(2)
    body = b"{}"
    assert security.sign_callback(1, body, 5) != security.sign_callback(2, body, 5)


def test_sending_is_signed_refuses_internal_hosts_and_does_not_follow_redirects(monkeypatch):
    from types import SimpleNamespace

    from app.services import verifications as service

    sent = []

    class FakeClient:
        def __init__(self, **kwargs):
            sent.append({"client": kwargs})

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, content=None, headers=None):
            sent.append({"url": url, "content": content, "headers": headers})

    class InlineThread:
        def __init__(self, target, daemon=None):
            self.target = target

        def start(self):
            self.target()

    import httpx
    import threading
    monkeypatch.setattr(httpx, "Client", FakeClient)
    monkeypatch.setattr(threading, "Thread", InlineThread)
    verification = SimpleNamespace(
        callback_url="https://client.example.com/kyc", account_id=7, reference="abc",
        steps=[], event="request.completed", status=SimpleNamespace(value="passed"), reasons=[],
    )

    resolves_to(monkeypatch, "93.184.216.34")
    service._fire_callback(None, verification)
    post = [s for s in sent if "url" in s][0]
    client_options = [s for s in sent if "client" in s][0]["client"]
    assert client_options["follow_redirects"] is False
    assert post["headers"]["X-KYC-Signature"] == security.sign_callback(
        7, post["content"], int(post["headers"]["X-KYC-Timestamp"])
    )
    assert json.loads(post["content"])["reference"] == "abc"

    sent.clear()
    resolves_to(monkeypatch, "169.254.169.254")   # the address has been changed since
    service._fire_callback(None, verification)
    assert not [s for s in sent if "url" in s], "must not call an internal address"


def test_the_callback_secret_endpoint_needs_a_login():
    from fastapi.testclient import TestClient

    from app.main import app
    assert TestClient(app).get("/v1/auth/callback-secret").status_code == 401


def test_a_stored_unsafe_redirect_is_not_handed_to_the_browser():
    assert urls.redirect_or_none("javascript:alert(1)") is None
    assert urls.redirect_or_none("https://client.example.com/done") == "https://client.example.com/done"
    assert urls.redirect_or_none(None) is None
