from datetime import date

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status

from app.api.v1.routes.verifications import open_session
from app.pipeline.document import scan_document
from app.rules import document as rules
from app.schemas.document import Applicant, DocumentCheckResponse, DocumentType

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
