"""The steps a person walks through, in the session they were sent.

These are reached with the session link, not an API key: the person holding the
link is the person being verified, and they may be on a phone that has never
heard of the client's credentials. Everything they do is recorded against the
verification, so the client sees it by polling and the report can be assembled
from rows afterwards.

The order is the one the client asked for at initiation. A step out of turn is
refused rather than quietly accepted, because a face matched against a document
nobody has scanned yet is not a verification of anything.
"""

import base64
import json
import time

import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    DocumentScan,
    FaceCheck,
    FaceTemplate,
    FileKind,
    Step,
    StepStatus,
    Verification,
    VerificationFile,
    VerificationStep,
)
from app.db.session import get_session
from app.engines.face import detect_faces, portrait
from app.engines.image import InvalidImageError, decode_image
from app.engines import recognition
from app.engines.recognition import embed, pass_threshold, similarity
from app.pipeline.address import scan_address_proof
from app.pipeline.document import scan_document
from app.pipeline.face_match import match_faces
from app.pipeline.liveness import check_liveness
from app.rules import address as address_rules
from app.rules import document as document_rules
from app.rules import face as face_rules
from app.schemas.address import AddressCheckResponse, ProofType
from app.schemas.document import Applicant as ApplicantDetails
from app.schemas.document import Decision, DocumentCheckResponse, DocumentType
from app.schemas.face import FaceVerificationResponse
from app.services import verifications as service
from app.storage import evidence, sessions

router = APIRouter(prefix="/sessions", tags=["session steps"])

_DECISION_TO_STATUS = {
    Decision.passed: StepStatus.passed,
    Decision.review: StepStatus.review,
    Decision.failed: StepStatus.failed,
    Decision.retake: StepStatus.failed,
}


def _open_session(db: Session, token: str) -> Verification:
    """The verification behind this link, or a refusal the person can act on."""
    verification = service.by_token(db, token)
    if verification is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That link is not valid")
    verification = service.expire_if_due(db, verification)
    if verification.status.value == "expired":
        raise HTTPException(status.HTTP_410_GONE, "This verification link has expired")
    return verification


def _require_turn(verification: Verification, step: VerificationStep) -> None:
    """Refuse a step the session did not ask for, or is not up to yet."""
    if step.value not in verification.services:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"This verification does not include the {step.value} step",
        )
    for name in verification.services:
        if name == step.value:
            return
        if not _passed(verification, VerificationStep(name)):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"The {name} step has to be completed first",
            )


def _passed(verification: Verification, step: VerificationStep) -> bool:
    return any(s.step == step and s.status == StepStatus.passed for s in verification.steps)


def _read(upload: UploadFile, field: str) -> bytes:
    if upload.content_type not in document_rules.ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"{field} must be one of: {', '.join(sorted(document_rules.ALLOWED_CONTENT_TYPES))}",
        )
    data = upload.file.read(document_rules.MAX_UPLOAD_BYTES + 1)
    if len(data) > document_rules.MAX_UPLOAD_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"{field} is larger than 10 MB")
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{field} is empty")
    return data


def _read_proof(upload: UploadFile, field: str) -> bytes:
    """The same as _read, plus PDFs: most bank statements only exist as one."""
    if upload.content_type not in address_rules.ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"{field} must be one of: {', '.join(sorted(address_rules.ALLOWED_CONTENT_TYPES))}",
        )
    data = upload.file.read(document_rules.MAX_UPLOAD_BYTES + 1)
    if len(data) > document_rules.MAX_UPLOAD_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"{field} is larger than 10 MB")
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{field} is empty")
    return data


def _keep_file(db: Session, verification: Verification, step: Step, kind: FileKind,
               data: bytes, content_type: str) -> None:
    stored = evidence.keep(verification.id, data, kind.value, content_type)
    if stored is None:
        return
    db.add(
        VerificationFile(
            verification_id=verification.id,
            step_id=step.id,
            kind=kind,
            storage_key=stored.storage_key,
            content_type=stored.content_type,
            byte_size=stored.byte_size,
            sha256=stored.sha256,
        )
    )


def _device(request: Request) -> dict:
    """What can be seen about the device from the request itself.

    The richer signals (ASN, proxy, fraud score) need a lookup service that is
    not wired up yet; this is what arrives for free, and it is enough to say the
    document was captured on a phone and the rest happened on a laptop.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    return {
        "ip": forwarded.split(",")[0].strip() or (request.client.host if request.client else None),
        "user_agent": request.headers.get("user-agent"),
        "language": request.headers.get("accept-language"),
    }


@router.post("/{token}/document", response_model=DocumentCheckResponse)
def submit_document(
    token: str,
    request: Request,
    document_type: DocumentType = Form(...),
    front: UploadFile = File(..., description="Photo page or front side"),
    back: UploadFile | None = File(None, description="Back side, where ID cards carry the MRZ"),
    db: Session = Depends(get_session),
) -> DocumentCheckResponse:
    """Read the person's document and record the step.

    What the client claimed at initiation is what the document is checked
    against: the names, the date of birth and the country they gave us. Nothing
    is taken from this request, so the person cannot type a different name into
    their own check.
    """
    verification = _open_session(db, token)
    _require_turn(verification, VerificationStep.document)

    front_bytes = _read(front, "front")
    back_bytes = _read(back, "back") if back else None
    claimed = verification.claimed or {}

    step = service.open_step(db, verification, VerificationStep.document)
    step.device = _device(request)

    started = time.perf_counter()
    result = scan_document(
        document_type=document_type,
        front=front_bytes,
        back=back_bytes,
        applicant=ApplicantDetails(
            first_name=claimed.get("first_name"),
            last_name=claimed.get("last_name"),
            date_of_birth=claimed.get("date_of_birth"),
            issuing_country=claimed.get("country"),
        ),
    )

    extracted = result.extracted.model_dump(mode="json") if result.extracted else {}
    extracted["document_type"] = document_type.value

    _keep_file(db, verification, step, FileKind.document_front, front_bytes, front.content_type)
    if back_bytes:
        _keep_file(db, verification, step, FileKind.document_back, back_bytes, back.content_type)

    # The portrait on the document is what the face step will compare against,
    # so it is cut and kept now, while the image is in hand.
    portrait_kept = _keep_document_portrait(db, verification, step, front_bytes)

    reasons = list(result.reasons)
    warnings = list(result.warnings)
    decision = result.decision

    # An identity document has a face on it. If we cannot find one, the step
    # stops here rather than letting the person walk on to a face check that has
    # nothing to compare against and fails for a reason they cannot act on. A
    # birth certificate is the exception: it carries no photograph at all.
    if portrait_kept == "missing" and document_type != DocumentType.birth_certificate:
        reasons.append("NO_PORTRAIT_ON_DOCUMENT")
        decision = Decision.retake
    elif portrait_kept == "small":
        warnings.append("DOCUMENT_PHOTO_SMALL")

    db.add(
        DocumentScan(
            step_id=step.id,
            verification_id=verification.id,
            applicant_id=verification.applicant_id,
            document_type=document_type,
            decision=decision,
            reasons=reasons,
            warnings=warnings,
            checks=[check.model_dump(mode="json") for check in result.checks],
            processing_ms=int((time.perf_counter() - started) * 1000),
            **{key: value for key, value in extracted.items() if key not in ("source", "document_type")},
            source=extracted.get("source"),
        )
    )

    service.finish_step(
        db,
        step,
        status=_DECISION_TO_STATUS[decision],
        reasons=reasons,
        warnings=warnings,
        result={"checks": [check.model_dump(mode="json") for check in result.checks],
                "portrait": portrait_kept},
        extracted=extracted,
    )
    _fill_applicant(db, verification, extracted)
    service.settle(db, verification)

    result.decision = decision
    result.reasons = list(dict.fromkeys(reasons))
    result.warnings = list(dict.fromkeys(warnings))
    result.portrait = portrait_kept
    result.verification_id = verification.reference
    return result


def _keep_document_portrait(db: Session, verification: Verification, step: Step, front: bytes) -> str:
    """Cut the face out of the document, keep it, and remember its numbers.

    The detector that decides whether there is a portrait is the one the match
    will use, not the one that happens to be quickest. Asking YuNet first meant a
    card it could not see a face in was recorded as having no portrait, even
    though the matcher's own detector could see one perfectly well, and the face
    step then had nothing to compare against.

    YuNet is still used for the crop when it agrees, because that crop is the one
    the pictures on screen have always used.
    """
    import cv2

    try:
        image = decode_image(front)
    except InvalidImageError:
        return "missing"

    found = recognition.find_face(image)
    if found is None:
        return "missing"

    box, landmarks = found
    width, height = float(box[2] - box[0]), float(box[3] - box[1])
    eye_distance = float(abs(landmarks[1][0] - landmarks[0][0]))
    small = (
        height < face_rules.SMALL_DOCUMENT_FACE_HEIGHT
        or eye_distance < face_rules.SMALL_DOCUMENT_EYE_DISTANCE
    )

    # The picture people are shown: YuNet's crop where it sees the same face,
    # otherwise the same framing around the box the matcher found.
    faces = detect_faces(image)
    cut = portrait(image, faces[0]) if faces else _crop_around(image, box)
    ok, buffer = cv2.imencode(".jpg", cut, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if ok:
        _keep_file(db, verification, step, FileKind.document_portrait, buffer.tobytes(), "image/jpeg")

    vector = embed(image)
    if vector is not None:
        db.add(
            FaceTemplate(
                account_id=verification.account_id,
                applicant_id=verification.applicant_id,
                verification_id=verification.id,
                source="document",
                embedding=[float(value) for value in vector],
            )
        )
    return "small" if small else "ok"


def _crop_around(image, box):
    """A third of the face box added on every side, the crop used everywhere."""
    x1, y1, x2, y2 = (int(value) for value in box[:4])
    pad_x, pad_y = (x2 - x1) // 3, (y2 - y1) // 3
    return image[max(0, y1 - pad_y): y2 + pad_y, max(0, x1 - pad_x): x2 + pad_x]


def _fill_applicant(db: Session, verification: Verification, extracted: dict) -> None:
    """Move what the document proved onto the person's record.

    The claimed values stay where they were. These are the verified ones, and
    they are what a later check, such as a child's certificate, is allowed to
    rely on.
    """
    applicant = verification.applicant
    if applicant is None or not extracted:
        return
    applicant.first_name = extracted.get("given_names") or applicant.first_name
    applicant.last_name = extracted.get("surname") or applicant.last_name
    applicant.date_of_birth = extracted.get("date_of_birth") or applicant.date_of_birth
    applicant.country = extracted.get("issuing_country") or applicant.country
    db.flush()


@router.post("/{token}/address", response_model=AddressCheckResponse)
def submit_address(
    token: str,
    request: Request,
    document_type: ProofType = Form(..., description="The kind of proof being uploaded"),
    proof: UploadFile = File(..., description="The bill, statement or agreement: an image or a PDF"),
    back: UploadFile | None = File(None, description="Second file, when the proof is in two"),
    db: Session = Depends(get_session),
) -> AddressCheckResponse:
    """Check a proof of address against the address the client gave us.

    **The address must be written on the document exactly as it was given to us.**
    The comparison is direct: differences are not tolerated. Case, spacing and
    punctuation are ignored, because those are how the text was printed rather than
    what it says, and the document's lines are joined first so an address printed
    across two lines still reads as one. Nothing else is forgiven: an abbreviation
    is not the word it stands for, and a street code is not the city it sits in.

    So a verification opened with `Kigali, Rwanda` **fails** against a utility bill
    that prints `KG 333 St`, with `ADDRESS_MISMATCH`. It is a failure, not a review:
    the two do not say the same address. The same bill against a verification
    opened with `KG 333 St` passes.

    Accepts a PDF as well as a photograph or a scan, because most bank statements
    only exist as a PDF. Uploaded, not captured: there is nothing for a camera
    guide to help with.

    The address comes from the verification, never from this request, and the name
    looked for is the one the document step proved. So the person can choose which
    document to upload, and nothing else about their own check.
    """
    verification = _open_session(db, token)
    _require_turn(verification, VerificationStep.address)

    claimed_address = (verification.claimed or {}).get("address")
    if not claimed_address:
        # Initiation refuses this, so reaching it means the verification was made
        # before that rule existed.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This verification was created without an address, so there is nothing to check against",
        )

    front_bytes = _read_proof(proof, "proof")
    back_bytes = _read_proof(back, "back") if back else None

    step = service.open_step(db, verification, VerificationStep.address)
    step.device = _device(request)

    applicant = verification.applicant
    result = scan_address_proof(
        proof_type=document_type,
        claimed_address=claimed_address,
        front=front_bytes,
        back=back_bytes,
        names=[applicant.first_name, applicant.last_name] if applicant else [],
    )

    _keep_file(db, verification, step, FileKind.address_proof, front_bytes, proof.content_type)
    if back_bytes:
        _keep_file(db, verification, step, FileKind.address_proof, back_bytes, back.content_type)

    service.finish_step(
        db,
        step,
        status=_DECISION_TO_STATUS[result.decision],
        reasons=result.reasons,
        warnings=result.warnings,
        result={"checks": [check.model_dump(mode="json") for check in result.checks]},
        extracted=result.extracted.model_dump(mode="json"),
    )
    service.settle(db, verification)
    return result


@router.post("/{token}/face", response_model=FaceVerificationResponse)
def submit_face(
    token: str,
    request: Request,
    frames: list[UploadFile] = File(..., description="Frames from the head movements, in order"),
    portrait_file: UploadFile | None = File(
        None, alias="portrait", description="The still taken with the person facing the camera"
    ),
    video: UploadFile | None = File(None, description="A few seconds of the challenge, kept as evidence"),
    challenge: str = Form(face_rules.DEFAULT_CHALLENGE),
    steps: str | None = Form(None, description="Which frame was captured for which step"),
    db: Session = Depends(get_session),
) -> FaceVerificationResponse:
    """Prove a live person is there, and that they are the one on the document."""
    verification = _open_session(db, token)
    _require_turn(verification, VerificationStep.face)

    if not face_rules.MIN_FRAMES <= len(frames) <= face_rules.MAX_FRAMES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Send between {face_rules.MIN_FRAMES} and {face_rules.MAX_FRAMES} frames, got {len(frames)}",
        )

    payloads = [_read(frame, f"frame {index}") for index, frame in enumerate(frames)]
    portrait_bytes = _read(portrait_file, "portrait") if portrait_file else None

    step = service.open_step(db, verification, VerificationStep.face)
    step.device = _device(request)

    started = time.perf_counter()
    liveness = check_liveness(
        payloads,
        challenge_type=challenge,
        resting=sessions.resting_for(token),
        claims=_parse_steps(steps),
    )

    # Identity is decided on the portrait, against the document's own.
    document_vector = _document_template(db, verification)
    captured = None
    if portrait_bytes:
        try:
            captured = embed(decode_image(portrait_bytes))
        except InvalidImageError:
            captured = None
    match = match_faces(
        np.asarray(document_vector, dtype=np.float32) if document_vector else None,
        [captured] if captured is not None else [],
    )

    if portrait_bytes:
        _keep_file(db, verification, step, FileKind.portrait, portrait_bytes, portrait_file.content_type)
    if video is not None:
        clip = video.file.read(face_rules.MAX_VIDEO_BYTES + 1)
        if len(clip) > face_rules.MAX_VIDEO_BYTES:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "video is too large")
        if video.content_type not in face_rules.ALLOWED_VIDEO_TYPES:
            raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "video must be webm, mp4 or quicktime")
        _keep_file(db, verification, step, FileKind.challenge_video, clip, video.content_type)

    duplicate, duplicate_score = (None, None)
    if captured is not None:
        duplicate, duplicate_score = _seen_before(db, verification, captured)
        db.add(
            FaceTemplate(
                account_id=verification.account_id,
                applicant_id=verification.applicant_id,
                verification_id=verification.id,
                source="portrait",
                embedding=[float(value) for value in captured],
            )
        )

    warnings = list(match.check.warnings)
    if duplicate is not None:
        # A warning, never a failure: the same face on another person's record is
        # worth a human looking, and is not proof of anything by itself.
        warnings.append("DUPLICATE_FACE_SEEN")

    db.add(
        FaceCheck(
            step_id=step.id,
            verification_id=verification.id,
            liveness_passed=liveness.decision == Decision.passed,
            challenge_completed=liveness.challenge.completed,
            planarity=liveness.planarity,
            match_score=match.score,
            match_threshold=match.threshold,
            matched=match.check.status.value == "pass",
            duplicate_of_verification_id=duplicate,
            duplicate_score=duplicate_score,
            result={"liveness": liveness.model_dump(mode="json", exclude={"capture"}),
                    "match": match.model_dump(mode="json")},
        )
    )

    passed = liveness.decision == Decision.passed and match.check.status.value == "pass"
    service.finish_step(
        db,
        step,
        status=StepStatus.passed if passed else StepStatus.failed,
        reasons=liveness.reasons + match.check.reasons,
        warnings=warnings,
        result={"match_score": match.score, "threshold": match.threshold},
    )
    verification = service.settle(db, verification)

    return FaceVerificationResponse(
        verification_id=verification.reference,
        decision=Decision.passed if passed else Decision.failed,
        reasons=list(dict.fromkeys(liveness.reasons + match.check.reasons)),
        warnings=list(dict.fromkeys(warnings)),
        liveness=liveness,
        match=match,
        document=None,
        processing_ms=int((time.perf_counter() - started) * 1000),
        redirect_url=verification.redirect_url,
    )


def _document_template(db: Session, verification: Verification) -> list | None:
    row = db.scalar(
        select(FaceTemplate)
        .where(FaceTemplate.verification_id == verification.id, FaceTemplate.source == "document")
        .order_by(FaceTemplate.id.desc())
    )
    return row.embedding if row else None


def _seen_before(db: Session, verification: Verification, vector) -> tuple[int | None, float | None]:
    """Has this face been used by a different person on this account?

    Compared in the application because pgvector is not installed; at this
    volume that is fine, and moving the comparison into the database later
    changes this function alone.
    """
    others = db.scalars(
        select(FaceTemplate).where(
            FaceTemplate.account_id == verification.account_id,
            FaceTemplate.source == "portrait",
            FaceTemplate.applicant_id != verification.applicant_id,
        )
    ).all()

    best, best_score = None, 0.0
    for other in others:
        score = similarity(vector, np.asarray(other.embedding, dtype=np.float32))
        if score > best_score:
            best, best_score = other, score

    # Deliberately stricter than the match line: at the match threshold this
    # would flag siblings. Until it is measured on real pairs it stays high.
    if best is not None and best_score >= pass_threshold() + face_rules.DUPLICATE_FACE_MARGIN:
        return best.verification_id, round(best_score, 4)
    return None, None


def _parse_steps(steps: str | None) -> dict[int, str] | None:
    if not steps:
        return None
    try:
        return {int(entry["index"]): str(entry["step"]) for entry in json.loads(steps)}
    except (ValueError, TypeError, KeyError):
        return None
