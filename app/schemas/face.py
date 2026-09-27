from pydantic import BaseModel

from app.schemas.document import CheckResult, Decision, ExtractedFields


class FramePose(BaseModel):
    index: int
    faces: int
    yaw: float | None = None
    pitch: float | None = None
    direction: str | None = None
    # How open the eyes were, so a still with shut eyes is not chosen.
    eye_aspect_ratio: float | None = None


class ChallengeProgress(BaseModel):
    type: str
    required: list[str]
    completed: list[str]
    missing: list[str]


class CapturedFace(BaseModel):
    """The still taken once the circle is complete: a frontal, sharp frame."""

    frame_index: int
    blur_score: float
    width: int
    height: int
    # The cropped face as a base64 JPEG, to pass straight into the face match.
    image_base64: str


class LivenessResponse(BaseModel):
    decision: Decision
    reasons: list[str]
    warnings: list[str]
    challenge: ChallengeProgress
    # The person's resting pose, which directions are measured against. Useful for tuning.
    baseline_yaw: float
    baseline_pitch: float
    # How much of the movement a flat surface cannot explain, as a percentage of
    # face size. Low means the camera was shown a photograph, not a person.
    planarity: float | None = None
    frames_received: int
    frames_with_face: int
    poses: list[FramePose]
    capture: CapturedFace | None
    checks: list[CheckResult]
    processing_ms: int


class FrameProbeResponse(BaseModel):
    """What one live camera frame looks like: good enough, and which way the head points."""

    usable: bool
    faces: int = 0
    yaw: float | None = None
    pitch: float | None = None
    # Only returned when the caller sends the resting pose it measured first.
    direction: str | None = None
    face_ratio: float | None = None
    blur_score: float | None = None
    brightness: float | None = None
    # How evenly the face is lit, 0 to 1 (ISO/IEC 29794-5 illumination uniformity).
    uniformity: float | None = None
    # How open the eyes are, used when choosing the still to keep.
    eye_aspect_ratio: float | None = None
    # NO_FACE, MULTIPLE_FACES, MOVE_CLOSER, MOVE_BACK, OFF_CENTRE, TOO_DARK,
    # TOO_BRIGHT, UNEVEN_LIGHTING, HOLD_STILL
    hints: list[str] = []


class FaceMatchResult(BaseModel):
    """How closely the captured face matches the portrait on the document."""

    # Cosine similarity between the two ArcFace vectors, -1 to 1: the mean of the
    # best MATCH_TOP_FRAMES frontal frames. No confidence percentage is reported,
    # because a similarity is not a probability and we have no calibration set to
    # turn one into the other.
    score: float | None
    best_score: float | None
    frames_compared: int
    threshold: float
    check: CheckResult


class FaceVerificationResponse(BaseModel):
    """Liveness and face match together, against a document scanned earlier."""

    verification_id: str
    decision: Decision
    reasons: list[str]
    warnings: list[str]
    liveness: LivenessResponse
    match: FaceMatchResult
    document: ExtractedFields | None
    processing_ms: int
