"""The last round of fixes: docs, session expiry, reason codes, image limits, approval and
quotas, email, and what a file's metadata says."""

import io
import smtplib

import numpy as np
import pytest
from PIL import Image

from app import main as app_main
from app.core import security
from app.core.config import settings
from app.engines import provenance
from app.engines.image import InvalidImageError, decode_image
from app.services import email as email_service
from tests.test_sessions import (  # noqa: F401  (fixtures)
    ACCOUNT, api_key, client, db_session, open_verification, token_of,
)


# L1: docs

@pytest.mark.parametrize("environment, setting, expected", [
    ("development", None, True), ("production", None, False),
    ("production", True, True), ("development", False, False),
])
def test_docs_follow_the_environment_unless_told_otherwise(monkeypatch, environment, setting, expected):
    monkeypatch.setattr(settings, "ENVIRONMENT", environment)
    monkeypatch.setattr(settings, "ENABLE_DOCS", setting)
    assert app_main.docs_enabled() is expected


# L3: the link's expiry is on the session view

def test_the_session_view_says_when_the_link_expires(client, api_key):
    body = open_verification(client, api_key)
    view = client.get(f"/v1/sessions/{token_of(body)}").json()
    assert view["expires_at"] is not None


# The ceiling stays at 30 days: clients (the Tuesday app sends ttl_days=20) rely on it.

def test_a_link_can_still_be_asked_to_last_twenty_days(client, api_key):
    response = client.post("/v1/verifications", json={"services": ["document"], "ttl_days": 20, "applicant": {}}, headers=api_key)
    assert response.status_code in (200, 201)
    assert settings.MAX_SESSION_TTL_MINUTES == 60 * 24 * 30


# L9: image size is checked before decoding

def _png(width, height):
    buffer = io.BytesIO()
    Image.fromarray(np.zeros((height, width, 3), dtype=np.uint8)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_an_image_over_the_limit_is_refused_before_it_is_opened(monkeypatch):
    monkeypatch.setattr(settings, "MAX_IMAGE_PIXELS", 10_000)
    assert decode_image(_png(50, 50)).shape[:2] == (50, 50)
    with pytest.raises(InvalidImageError):
        decode_image(_png(200, 200))


def test_an_image_with_one_very_long_side_is_refused(monkeypatch):
    monkeypatch.setattr(settings, "MAX_IMAGE_SIDE_PX", 100)
    with pytest.raises(InvalidImageError):
        decode_image(_png(300, 10))


# M7: quotas

def _bearer(client, email="new@tuesday.africa"):
    token = client.post("/v1/auth/register", json={**ACCOUNT, "email": email}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_an_account_can_only_hold_so_many_keys(client, monkeypatch):
    monkeypatch.setattr(settings, "MAX_API_KEYS_PER_ACCOUNT", 2)
    bearer = _bearer(client)
    assert [client.post("/v1/api-keys", json={"name": str(i)}, headers=bearer).status_code for i in range(3)] == [201, 201, 409]


def test_an_account_has_a_daily_limit_on_verifications(client, api_key, monkeypatch):
    monkeypatch.setattr(settings, "MAX_VERIFICATIONS_PER_DAY", 2)
    codes = [client.post("/v1/verifications", json={"services": ["document"], "applicant": {}}, headers=api_key).status_code
             for _ in range(3)]
    assert codes[:2] == [200, 200] or codes[:2] == [201, 201]
    assert codes[2] == 429


# M11: email

def test_a_reset_is_sent_by_smtp_when_it_is_configured(monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent["host"] = (host, port)
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self, context=None): sent["tls"] = True
        def login(self, user, password): sent["login"] = user
        def send_message(self, message): sent["to"], sent["body"] = message["To"], message.get_content()

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    for name, value in {"EMAIL_ENABLED": True, "SMTP_HOST": "smtp.test", "SMTP_USER": "u", "SMTP_PASSWORD": "p"}.items():
        monkeypatch.setattr(settings, name, value)
    assert email_service.send_password_reset("jean@tuesday.africa", "TOKEN123") is True
    assert sent["to"] == "jean@tuesday.africa" and "TOKEN123" in sent["body"] and sent["tls"] and sent["login"] == "u"


def test_a_failed_send_reports_false_and_does_not_raise(monkeypatch):
    def broken(*a, **k):
        raise OSError("down")
    monkeypatch.setattr(smtplib, "SMTP", broken)
    monkeypatch.setattr(settings, "EMAIL_ENABLED", True)
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.test")
    assert email_service.send_password_reset("a@b.co", "T") is False


def test_start_up_never_refuses_for_missing_email(monkeypatch):
    monkeypatch.setattr(security, "check_evidence_store", lambda: None)
    monkeypatch.setattr(security, "check_schema", lambda: None)
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "JWT_SECRET", "x" * 40)
    monkeypatch.setattr(settings, "EMAIL_ENABLED", False)
    security.check_configuration()          # a warning in the log, not an error


# H5b: what the file's own history says

def _pdf(**meta):
    buffer = io.BytesIO()
    Image.new("RGB", (60, 60), "white").save(buffer, format="PDF", **meta)
    return buffer.getvalue()


def test_a_pdf_made_by_an_editor_is_flagged():
    assert "EDITING_SOFTWARE_DETECTED" in provenance.signals(_pdf(producer="Adobe Photoshop 25.0"))


def test_an_ordinary_pdf_has_no_signals():
    assert provenance.signals(_pdf(producer="Microsoft Reporting Services")) == {}


def test_a_pdf_saved_twice_is_flagged():
    data = _pdf()
    assert "SAVED_MORE_THAN_ONCE" in provenance.signals(data + b"\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n")


def test_a_photo_that_names_an_editor_is_flagged():
    buffer = io.BytesIO()
    image = Image.new("RGB", (40, 40), "white")
    exif = Image.Exif()
    exif[0x0131] = "GIMP 2.10"
    image.save(buffer, format="JPEG", exif=exif)
    assert provenance.signals(buffer.getvalue()) == {"EDITING_SOFTWARE_DETECTED": "gimp"}


def test_reason_codes_for_dates_are_distinct():
    from datetime import date
    from app.pipeline.address import _recency_check
    from app.schemas.address import AddressExtracted, ProofType

    today = date(2026, 10, 16)
    extracted = AddressExtracted(document_type=ProofType.utility_bill, full_address="x")
    future = _recency_check(["Statement date 15 December 2026"], today, extracted)
    none = _recency_check(["no dates here"], today, extracted)
    assert future.reasons == ["PROOF_DATE_IN_FUTURE"] and none.reasons == ["PROOF_DATE_NOT_FOUND"]
