import base64
import json
import time
from pathlib import Path

import cv2

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status

from app.engines.face import crop_face, detect_faces, portrait
from app.engines.image import InvalidImageError, decode_image
from app.engines.recognition import embed
from app.pipeline.face_match import match_faces
from app.pipeline.liveness import check_liveness
from app.rules import document as document_rules
from app.core.config import settings
from app.rules import face as rules
from app.schemas.document import CheckStatus, Decision, ExtractedFields
from app.schemas.face import FaceVerificationResponse
from app.storage import sessions

router = APIRouter(prefix="/verifications")


def open_session(document_type: str, decision: str, fields: ExtractedFields | None, front: bytes):
    """Keep the document's portrait so the face step can reuse it.

    Returns the session plus what happened to the portrait, so the caller can say
    whether there was no face at all or one too small to match against.
    """
    face_embedding, face_image, quality = None, None, "missing"
    try:
        image = decode_image(front)
    except InvalidImageError:
        image = None

    if image is not None:
        found = detect_faces(image)
        if found:
            face = found[0]
            eyes = face.landmarks
            eye_distance = abs(eyes.left_eye[0] - eyes.right_eye[0])
            # Small portraits carry less detail, so the result says so, but the
            # check still runs: see rules/face.py.
            small = (
                face.height < rules.SMALL_DOCUMENT_FACE_HEIGHT
                or eye_distance < rules.SMALL_DOCUMENT_EYE_DISTANCE
            )
            quality = "small" if small else "ok"
            face_embedding = embed(image)
            ok, buffer = cv2.imencode(
                ".jpg", portrait(image, face), [cv2.IMWRITE_JPEG_QUALITY, 90]
            )
            face_image = base64.b64encode(buffer.tobytes()).decode() if ok else None

    session = sessions.create(
        document_face_small=quality == "small",
        document_type=document_type,
        document_decision=decision,
        document_fields=fields.model_dump(mode="json") if fields else {},
        document_embedding=face_embedding,
        document_face_base64=face_image,
    )
    return session, quality


@router.post("/{verification_id}/face", response_model=FaceVerificationResponse)
def verify_face(
    verification_id: str,
    frames: list[UploadFile] = File(..., description="Frames captured during the head-turn challenge, in order"),
    challenge: str = Form(rules.DEFAULT_CHALLENGE),
    steps: str | None = Form(
        None,
        description='Which frame was captured for which step: [{"index": 4, "step": "left"}]. '
        "Each named frame is measured by the server, so this cannot claim a movement that did not happen.",
    ),
) -> FaceVerificationResponse:
    started = time.perf_counter()
    session = sessions.get(verification_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Verification not found or expired")
    if challenge not in rules.CHALLENGES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"challenge must be one of: {', '.join(rules.CHALLENGES)}")
    if not rules.MIN_FRAMES <= len(frames) <= rules.MAX_FRAMES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Send between {rules.MIN_FRAMES} and {rules.MAX_FRAMES} frames, got {len(frames)}",
        )

    payloads = []
    for index, frame in enumerate(frames):
        data = frame.file.read(document_rules.MAX_UPLOAD_BYTES + 1)
        if len(data) > document_rules.MAX_UPLOAD_BYTES:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"frame {index} is larger than 10 MB")
        payloads.append(data)

    if settings.DEBUG_FRAMES_DIR:
        _dump_frames(verification_id, payloads, session)

    # The resting pose learned while the camera was being checked: the same
    # reference the live guidance used, so both agree on what counts as a turn.
    claims = _parse_steps(steps)
    liveness = check_liveness(
        payloads, challenge_type=challenge, resting=session.resting_mesh, claims=claims
    )

    # Identity is decided on the frames where the person faced the camera, and on
    # nothing else. The frames from the head movements prove liveness, not
    # identity: a turned face scores lower against a passport-style portrait
    # whoever it belongs to. Frontality is measured in degrees rather than taken
    # from the liveness step's "centre" label, which tolerates up to 18 degrees
    # of yaw (see rules/face.py FRONTAL_PITCH_DEGREES).
    candidates = [
        pose.index
        for pose in liveness.poses
        if abs(pose.yaw) <= rules.FRONTAL_DEGREES and abs(pose.pitch) <= rules.FRONTAL_PITCH_DEGREES
    ]

    scored = []
    for index in candidates:
        try:
            image = decode_image(payloads[index])
        except InvalidImageError:
            continue
        found = detect_faces(image)
        if len(found) != 1:
            continue
        crop = crop_face(image, found[0], margin=0.1)
        sharpness = float(cv2.Laplacian(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())
        scored.append((sharpness, index, image))

    embeddings = []
    for _, _, image in sorted(scored, key=lambda item: item[0], reverse=True)[: rules.MATCH_FRAMES]:
        # The matcher runs its own detection and alignment, so the frame goes in
        # whole: both sides of the comparison are then prepared the same way.
        embedding = embed(image)
        if embedding is not None:
            embeddings.append(embedding)

    match = match_faces(session.document_embedding, embeddings)
    if session.document_face_small:
        match.check.warnings.append("DOCUMENT_PHOTO_SMALL")

    reasons = liveness.reasons + match.check.reasons
    warnings = liveness.warnings + match.check.warnings
    document_problem = {"NO_FACE_ON_DOCUMENT"} & set(match.check.reasons)
    if document_problem:
        # Nothing the person can fix by standing still: the document must be rescanned.
        decision = Decision.retake
    elif match.check.status == CheckStatus.failed:
        decision = Decision.failed
    elif liveness.decision != Decision.passed:
        decision = liveness.decision
    elif match.check.status == CheckStatus.review:
        decision = Decision.review
    else:
        decision = Decision.passed

    session.face_result = {"decision": decision.value, "score": match.score, "reasons": reasons}
    return FaceVerificationResponse(
        verification_id=verification_id,
        decision=decision,
        reasons=list(dict.fromkeys(reasons)),
        warnings=list(dict.fromkeys(warnings)),
        liveness=liveness,
        match=match,
        document=ExtractedFields(**session.document_fields) if session.document_fields else None,
        processing_ms=int((time.perf_counter() - started) * 1000),
    )


def _dump_frames(verification_id: str, payloads: list[bytes], session) -> None:
    """Keep this run's frames on disk, for working out why a score came out as it did."""
    folder = Path(settings.DEBUG_FRAMES_DIR) / f"{int(time.time())}_{verification_id}"
    folder.mkdir(parents=True, exist_ok=True)
    for index, data in enumerate(payloads):
        (folder / f"frame_{index:02d}.jpg").write_bytes(data)
    if session.document_face_base64:
        (folder / "document_face.jpg").write_bytes(base64.b64decode(session.document_face_base64))


def _parse_steps(steps: str | None) -> dict[int, str] | None:
    """Read the frame-to-step map the guidance sends, ignoring anything malformed:
    a bad map should cost the person a retake, not a 500."""
    if not steps:
        return None
    try:
        entries = json.loads(steps)
        return {int(entry["index"]): str(entry["step"]) for entry in entries}
    except (ValueError, TypeError, KeyError):
        return None
