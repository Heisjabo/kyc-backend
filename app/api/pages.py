"""Test pages for driving the API by hand: scan a document, then check the face.

Not part of the service's API. They exist so the flow can be tried end to end in
a browser before the real frontend is built.
"""

from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse, HTMLResponse

from app.storage import sessions

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

router = APIRouter(include_in_schema=False)


@router.get("/", response_class=HTMLResponse)
def scan_page() -> HTMLResponse:
    return HTMLResponse((WEB_DIR / "scan.html").read_text())


@router.get("/capture", response_class=HTMLResponse)
def capture_page() -> HTMLResponse:
    """Guided document capture: the camera holds off until the card is readable."""
    return HTMLResponse((WEB_DIR / "capture.html").read_text())


@router.get("/verify/{verification_id}", response_class=HTMLResponse)
def verify_page(verification_id: str) -> HTMLResponse:
    return HTMLResponse((WEB_DIR / "verify.html").read_text())


@router.get("/static/style.css")
def stylesheet() -> FileResponse:
    return FileResponse(WEB_DIR / "style.css", media_type="text/css")


@router.get("/api/session/{verification_id}")
def session_summary(verification_id: str) -> dict:
    """What the face page shows about the document already scanned."""
    session = sessions.get(verification_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Verification not found or expired")
    return {
        "document_type": session.document_type,
        "document_decision": session.document_decision,
        "document_face": session.document_face_base64,
        "fields": {
            key: value
            for key, value in session.document_fields.items()
            if key in {"full_name", "document_number", "date_of_birth", "expiry_date", "issuing_country", "source"}
        },
    }
