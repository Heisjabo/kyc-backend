from datetime import date

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status

from app.api.v1.routes.verifications import open_session
from app.pipeline.document import scan_document
from app.pipeline.document_probe import probe_document_frame
from app.rules import document as rules
from app.schemas.document import Applicant, DocumentCheckResponse, DocumentFrameProbe, DocumentType

router = APIRouter(prefix="/document")


def _read_upload(upload: UploadFile, field: str) -> bytes:
    if upload.content_type not in rules.ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"{field} must be one of: {', '.join(sorted(rules.ALLOWED_CONTENT_TYPES))}",
        )
    data = upload.file.read(rules.MAX_UPLOAD_BYTES + 1)
    if len(data) > rules.MAX_UPLOAD_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"{field} is larger than 10 MB")
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{field} is empty")
    return data


# A plain `def` (not async): FastAPI runs it in a worker thread, so the
# CPU-heavy OCR doesn't block other requests.
@router.post("/scan", response_model=DocumentCheckResponse)
def scan(
    request: Request,
    document_type: DocumentType = Form(...),
    front: UploadFile = File(..., description="Photo page (passport) or front side (ID card)"),
    back: UploadFile | None = File(None, description="Back side, where ID cards carry the MRZ"),
    first_name: str | None = Form(None),
    last_name: str | None = Form(None),
    date_of_birth: date | None = Form(None, description="YYYY-MM-DD"),
    issuing_country: str | None = Form(None, description="Country code or name: RW, RWA or Rwanda"),
) -> DocumentCheckResponse:
    front_bytes = _read_upload(front, "front")
    result = scan_document(
        document_type=document_type,
        front=front_bytes,
        back=_read_upload(back, "back") if back else None,
        applicant=Applicant(
            first_name=first_name,
            last_name=last_name,
            date_of_birth=date_of_birth,
            issuing_country=issuing_country,
        ),
    )

    # Hold on to the document so the face step can compare against its portrait.
    session, portrait = open_session(document_type.value, result.decision.value, result.extracted, front_bytes)
    result.portrait = portrait
    if portrait != "missing":
        result.verification_id = session.id
        result.face_verification_url = str(request.base_url).rstrip("/") + f"/verify/{session.id}"
    if portrait == "small":
        result.warnings.append("DOCUMENT_PHOTO_SMALL")
    return result


@router.post("/frame", response_model=DocumentFrameProbe)
def frame(
    frame: UploadFile = File(..., description="One camera frame, cropped to the guide rectangle"),
    side: str = Form("front", description='"front" or "back". A back carries no portrait, so that check is skipped.'),
) -> DocumentFrameProbe:
    """Live guidance while a document is being held up to the camera.

    Called several times a second by the capture page, so it does everything but
    read the document: OCR is left to /scan on the captured still.
    """
    if side not in ("front", "back"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, 'side must be "front" or "back"')
    return probe_document_frame(_read_upload(frame, "frame"), side=side)


# A child's check is a separate endpoint on purpose. The document is different
# (a birth certificate, with no photograph, no expiry and no machine readable
# zone), the applicant details are compulsory rather than optional, and the age
# rule is turned off. Folding that into /scan with a flag would mean one endpoint
# whose rules change under the caller, which is how a check gets skipped by
# accident.
@router.post("/birth-certificate", response_model=DocumentCheckResponse)
def birth_certificate(
    request: Request,
    front: UploadFile = File(..., description="The birth certificate"),
    first_name: str = Form(..., description="The child's first name, as registered"),
    last_name: str = Form(..., description="The child's last name, as registered"),
    date_of_birth: date = Form(..., description="YYYY-MM-DD, checked against the certificate"),
    issuing_country: str = Form(..., description="Country code or name: RW, RWA or Rwanda"),
    back: UploadFile | None = File(None, description="Back of the certificate, if it has one"),
) -> DocumentCheckResponse:
    """Check a child's birth certificate.

    Every check the adult flow runs is run here too: the document is detected and
    read, the quality is judged, the names are compared and the date of birth is
    matched against the certificate. The one difference is that being under 18
    is not a failure, because a birth certificate is a child's document.

    The names, date of birth and country are required, not optional: there is no
    machine readable zone on a certificate to fall back on, so a check with
    nothing to compare against would confirm nothing.
    """
    front_bytes = _read_upload(front, "front")
    result = scan_document(
        document_type=DocumentType.birth_certificate,
        front=front_bytes,
        back=_read_upload(back, "back") if back else None,
        applicant=Applicant(
            first_name=first_name,
            last_name=last_name,
            date_of_birth=date_of_birth,
            issuing_country=issuing_country,
        ),
        require_adult=False,
    )

    # A certificate carries no photograph, which is normal and not worth saying.
    # The session still opens, because a child's face step is a liveness check
    # with a portrait kept at the end of it, not a match against the document.
    session, portrait = open_session(
        DocumentType.birth_certificate.value, result.decision.value, result.extracted, front_bytes
    )
    result.portrait = portrait
    result.verification_id = session.id
    result.face_verification_url = str(request.base_url).rstrip("/") + f"/verify/{session.id}"
    if portrait == "small":
        result.warnings.append("DOCUMENT_PHOTO_SMALL")
    return result
