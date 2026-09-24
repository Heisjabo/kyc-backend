"""Live feedback for one camera frame while the capture is being guided.

The page sends frames as they come, and this says whether the face is usable and
which way the head is pointing, so the guide only moves on once the movement has
actually happened and the person is told about bad light or blur while they can
still fix it.
"""

import cv2

from app.engines.face import crop_face, detect_faces
from app.engines.mesh import Mesh, face_mesh, head_rotation
from app.engines.signals import direction_of
from app.engines.image import InvalidImageError, decode_image, to_gray
from app.rules import face as rules
from app.schemas.face import FrameProbeResponse

_CROP_WIDTH = 200


def probe_frame(data: bytes, resting: Mesh | None = None) -> tuple[FrameProbeResponse, Mesh | None]:
    """Judge one live frame. Returns the answer and this frame's mesh, which the
    caller keeps as the resting pose while the camera is still being checked."""
    try:
        image = decode_image(data)
    except InvalidImageError:
        return FrameProbeResponse(usable=False, hints=["FRAME_UNREADABLE"]), None

    faces = detect_faces(image)
    if not faces:
        return FrameProbeResponse(usable=False, faces=0, hints=["NO_FACE"]), None
    if len(faces) > 1:
        return FrameProbeResponse(usable=False, faces=len(faces), hints=["MULTIPLE_FACES"]), None

    face = faces[0]
    short_side = min(image.shape[:2])
    ratio = face.height / short_side

    # Quality is measured on the face itself; the background behind it is irrelevant.
    crop = crop_face(image, face, margin=0.1)
    gray = to_gray(cv2.resize(crop, (_CROP_WIDTH, max(1, int(_CROP_WIDTH * crop.shape[0] / crop.shape[1])))))
    blur_score = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    brightness = float(gray.mean())

    # Illumination uniformity: one side of the face much darker than the other
    # means side lighting, which hides half the detail the match depends on.
    half = gray.shape[1] // 2
    left, right = float(gray[:, :half].mean()), float(gray[:, half:].mean())
    uniformity = min(left, right) / max(left, right, 1.0)

    # Framing: the face should sit in the middle of the frame.
    frame_height, frame_width = image.shape[:2]
    off_centre = max(
        abs(face.center[0] - frame_width / 2) / frame_width,
        abs(face.center[1] - frame_height / 2) / frame_height,
    )

    hints = []
    if ratio < rules.LIVE_MIN_FACE_RATIO:
        hints.append("MOVE_CLOSER")
    elif ratio > rules.LIVE_MAX_FACE_RATIO:
        hints.append("MOVE_BACK")
    if brightness < rules.LIVE_MIN_BRIGHTNESS:
        hints.append("TOO_DARK")
    elif brightness > rules.LIVE_MAX_BRIGHTNESS:
        hints.append("TOO_BRIGHT")
    if uniformity < rules.LIVE_MIN_UNIFORMITY:
        hints.append("UNEVEN_LIGHTING")
    if blur_score < rules.LIVE_MIN_BLUR:
        hints.append("HOLD_STILL")
    if off_centre > rules.LIVE_MAX_OFF_CENTRE:
        hints.append("OFF_CENTRE")

    mesh = face_mesh(image, face)
    yaw = pitch = None
    heading = None
    if mesh is not None and resting is not None:
        yaw, pitch, _ = head_rotation(resting, mesh)
        heading = direction_of(yaw, pitch)

    return FrameProbeResponse(
        usable=not hints,
        faces=1,
        yaw=yaw,
        pitch=pitch,
        direction=heading,
        eye_aspect_ratio=round(mesh.eye_aspect_ratio, 3) if mesh else None,
        face_ratio=round(ratio, 3),
        blur_score=round(blur_score, 1),
        brightness=round(brightness, 1),
        uniformity=round(uniformity, 3),
        hints=hints,
    ), mesh
