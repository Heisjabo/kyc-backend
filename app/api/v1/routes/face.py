from fastapi import Depends, APIRouter, File, Form, HTTPException, UploadFile, status

from app.pipeline.frame_probe import probe_frame
from app.pipeline.liveness import check_liveness
from app.rules import document as document_rules
from app.rules import face as rules
from app.schemas.face import FrameProbeResponse, LivenessResponse
from app.storage import sessions

from app.api.deps import legacy_only

router = APIRouter(prefix="/face", dependencies=[Depends(legacy_only)])


# A plain `def`: FastAPI runs it in a worker thread, so face detection on a
# burst of frames doesn't block other requests.
@router.post("/liveness", response_model=LivenessResponse)
def liveness(
    frames: list[UploadFile] = File(
        ...,
        description="Frames captured while the person moves their head, in order",
    ),
    challenge: str = Form(
        rules.DEFAULT_CHALLENGE,
        description=f"Which movements are required: {' or '.join(rules.CHALLENGES)}",
    ),
) -> LivenessResponse:
    if challenge not in rules.CHALLENGES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"challenge must be one of: {', '.join(rules.CHALLENGES)}",
        )
    if not rules.MIN_FRAMES <= len(frames) <= rules.MAX_FRAMES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Send between {rules.MIN_FRAMES} and {rules.MAX_FRAMES} frames, got {len(frames)}",
        )

    payloads = []
    for index, frame in enumerate(frames):
        if frame.content_type not in document_rules.ALLOWED_CONTENT_TYPES:
            raise HTTPException(
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                f"frame {index} must be one of: {', '.join(sorted(document_rules.ALLOWED_CONTENT_TYPES))}",
            )
        data = frame.file.read(document_rules.MAX_UPLOAD_BYTES + 1)
        if len(data) > document_rules.MAX_UPLOAD_BYTES:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"frame {index} is larger than 10 MB")
        payloads.append(data)

    return check_liveness(payloads, challenge_type=challenge)


@router.post("/frame", response_model=FrameProbeResponse)
def frame(
    frame: UploadFile = File(..., description="One camera frame, for live guidance"),
    verification_id: str | None = Form(None, description="Ties the frame to a scanned document"),
    calibrate: bool = Form(False, description="True while the camera is being checked, to learn the resting pose"),
) -> FrameProbeResponse:
    if frame.content_type not in document_rules.ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"frame must be one of: {', '.join(sorted(document_rules.ALLOWED_CONTENT_TYPES))}",
        )
    data = frame.file.read(document_rules.MAX_UPLOAD_BYTES + 1)
    if len(data) > document_rules.MAX_UPLOAD_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "frame is larger than 10 MB")

    # Movement is measured against this person's resting pose, learned while the
    # camera is being checked. It is held against whatever the page calls itself,
    # which is an in-memory session id in the old flow and a session token in a
    # verification: both are opaque strings and neither is worth keeping once the
    # person walks away.
    session = sessions.get(verification_id) if verification_id else None
    resting = session.resting_mesh if session else sessions.resting_for(verification_id)

    answer, mesh = probe_frame(data, resting)
    if calibrate and mesh is not None and answer.usable:
        if session is not None:
            session.resting_mesh = mesh
        elif verification_id:
            sessions.remember_resting(verification_id, mesh)
    return answer
