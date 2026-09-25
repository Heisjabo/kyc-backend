"""A child's check: a birth certificate, on its own endpoint.

The certificate is drawn here rather than photographed, so these test the rules
rather than the OCR, but the text is read by the real reader.
"""

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

DETAILS = {
    "first_name": "Divine",
    "last_name": "Uwase",
    "date_of_birth": "2015-06-12",
    "issuing_country": "RW",
}


def certificate(name: str = "UWASE DIVINE", dob: str = "12-06-2015", heading: str = "BIRTH CERTIFICATE") -> bytes:
    image = np.full((900, 1400, 3), 245, dtype=np.uint8)

    def line(text, y, scale=1.0, thickness=2):
        cv2.putText(image, text, (70, y), cv2.FONT_HERSHEY_SIMPLEX, scale, (20, 20, 20), thickness, cv2.LINE_AA)

    line("REPUBLIC OF RWANDA", 90, 1.3, 3)
    line(heading, 170, 1.5, 3)
    line("Names: " + name, 300)
    line("Sex: F", 370)
    line("Date of birth: " + dob, 440)
    line("Place of birth: KIGALI", 510)
    line("Father: UWIMANA JOSEPH", 580)
    line("Mother: MUKAMANA ALICE", 650)
    line("Registered: 20-06-2015", 720)
    return cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])[1].tobytes()


def send(data: bytes = None, **overrides):
    form = {**DETAILS, **overrides}
    return client.post(
        "/v1/document/birth-certificate",
        data=form,
        files={"front": ("certificate.jpg", data or certificate(), "image/jpeg")},
    )


def test_a_child_passes_where_the_adult_check_would_refuse_them():
    """The whole point of the endpoint: being under 18 is not a failure here."""
    body = send().json()
    assert body["decision"] == "pass", (body["reasons"], body["warnings"])
    assert "UNDERAGE" not in body["reasons"]
    assert "AGE_CHECK_NOT_REQUIRED" in body["warnings"]


def test_the_same_certificate_fails_the_adult_endpoint():
    """Proof the two paths really are separate, rather than one with a flag."""
    body = client.post(
        "/v1/document/scan",
        data={"document_type": "birth_certificate", **DETAILS},
        files={"front": ("certificate.jpg", certificate(), "image/jpeg")},
    ).json()
    assert body["decision"] == "fail"
    assert "UNDERAGE" in body["reasons"]


def test_every_other_check_still_runs():
    checks = {check["name"]: check["status"] for check in send().json()["checks"]}
    assert checks["document_detection"] == "pass"
    assert checks["document_type"] == "pass"
    assert checks["name_match"] == "pass"
    assert checks["date_of_birth_match"] == "pass"
    assert checks["issuing_country"] == "pass"


def test_a_date_of_birth_that_disagrees_with_the_certificate_fails():
    body = send(date_of_birth="2016-01-01").json()
    assert body["decision"] == "fail"
    assert "DOB_MISMATCH" in body["reasons"]


def test_a_name_that_disagrees_with_the_certificate_is_caught():
    body = send(first_name="Aline", last_name="Keza").json()
    assert body["decision"] in ("fail", "review")
    assert any("NAME" in reason for reason in body["reasons"])


@pytest.mark.parametrize("missing", ["first_name", "last_name", "date_of_birth", "issuing_country"])
def test_the_details_are_required(missing):
    """Nothing on a certificate can be checked against itself: no MRZ, no chip.
    Without the details there is nothing to compare, so the request is refused."""
    form = {key: value for key, value in DETAILS.items() if key != missing}
    response = client.post(
        "/v1/document/birth-certificate",
        data=form,
        files={"front": ("certificate.jpg", certificate(), "image/jpeg")},
    )
    assert response.status_code == 422


def test_a_document_that_is_not_a_certificate_is_refused():
    body = send(certificate(heading="DRIVING LICENCE")).json()
    assert body["decision"] in ("fail", "review")
    assert any("TYPE" in reason for reason in body["reasons"] + body["warnings"])


def test_the_face_step_opens_and_nothing_is_warned_about():
    """A certificate has no photograph and no expiry. Both are normal on this
    document, so neither is reported: a warning nobody can act on is noise."""
    body = send().json()
    assert body["portrait"] == "missing"
    assert body["face_verification_url"]
    assert "NO_PORTRAIT_ON_CERTIFICATE" not in body["warnings"]
    assert "EXPIRY_DATE_NOT_FOUND" not in body["warnings"]


def test_a_document_that_should_expire_still_reports_a_missing_expiry():
    """The silence is for certificates only, not a blanket change."""
    body = client.post(
        "/v1/document/scan",
        data={"document_type": "id_card", **DETAILS, "date_of_birth": "2002-02-24"},
        files={"front": ("certificate.jpg", certificate(heading="NATIONAL IDENTITY CARD"), "image/jpeg")},
    ).json()
    assert "EXPIRY_DATE_NOT_FOUND" in body["warnings"]


def test_it_is_in_the_api_schema():
    schema = client.get("/openapi.json").json()
    assert "/v1/document/birth-certificate" in schema["paths"]
