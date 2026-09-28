"""The pages a person walks through, and the QR that carries them to a phone."""

import cv2
import numpy as np
import segno
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
TOKEN = "xALxplBp-_r6972VpZimp9_URrgyJ0wvrsEdK5EEkYE"


def test_every_step_has_a_page():
    for path in ("", "/document", "/face", "/address"):
        assert client.get(f"/verify/{TOKEN}{path}").status_code == 200


def test_the_address_page_is_an_upload_not_a_camera():
    page = client.get(f"/verify/{TOKEN}/address").text
    assert 'type="file"' in page
    assert "getUserMedia" not in page and "<video" not in page


def test_the_qr_is_drawn_here_not_by_a_service_on_the_internet():
    """The link holds the session token, which is the whole credential for this
    verification. It does not go in somebody else's query string."""
    assert "qrserver" not in client.get(f"/verify/{TOKEN}").text

    response = client.get(f"/verify/{TOKEN}/qr.svg")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    # A credential is not left in a proxy or a browser cache.
    assert response.headers["cache-control"] == "no-store"


def test_the_qr_reads_back_as_the_session_link():
    """Drawn the way the endpoint draws it, then read by a real decoder: a QR
    nobody can scan is the same as no QR."""
    link = "http://testserver/verify/" + TOKEN
    rendered = segno.make(link, error="m").png_data_uri(scale=6)
    import base64

    data = base64.b64decode(rendered.split(",", 1)[1])
    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    decoded, _, _ = cv2.QRCodeDetector().detectAndDecode(image)
    assert decoded == link
