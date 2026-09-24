"""Head pose in degrees, the way the standards express it.

ISO/IEC 29794-5 (face image quality) treats head pose as yaw, pitch and roll in
degrees, and BSI's OFIQ, its reference implementation, reports those angles. The
usual way to get them from a photo is to fit a generic 3D head to the landmarks
the detector found and read back the rotation (OpenCV's solvePnP), which is what
this does with YuNet's five points.

The angles are approximate: five points and an assumed focal length cannot give
survey accuracy, and the absolute values shift with camera position and face
shape. That is why the challenge compares each person with their own resting
pose rather than with fixed angles.
"""

from dataclasses import dataclass

import cv2
import numpy as np

from app.rules import face as rules

# A generic head, in millimetres, for the five points YuNet returns. X is to the
# right of the image, Y down, Z towards the camera; the nose tip is the origin.
_MODEL_POINTS = np.array(
    [
        (-32.0, -40.0, -30.0),   # right eye centre (left side of the image)
        (32.0, -40.0, -30.0),    # left eye centre
        (0.0, 0.0, 0.0),         # nose tip
        (-28.0, 32.0, -25.0),    # right mouth corner
        (28.0, 32.0, -25.0),     # left mouth corner
    ],
    dtype=np.float64,
)


@dataclass
class PoseAngles:
    """Degrees. yaw: turning left (negative) or right (positive) as the image sees it.
    pitch: chin up is negative, chin down positive. roll: head tilted sideways."""

    yaw: float
    pitch: float
    roll: float


def estimate_pose(landmarks, image_shape: tuple[int, int]) -> PoseAngles | None:
    """Fit the generic head to these five points and read back the rotation."""
    points = np.array(
        [
            landmarks.right_eye,
            landmarks.left_eye,
            landmarks.nose,
            landmarks.right_mouth,
            landmarks.left_mouth,
        ],
        dtype=np.float64,
    )

    height, width = image_shape[:2]
    # Focal length is unknown for an arbitrary webcam; the image width is the
    # standard stand-in, and the principal point is taken as the image centre.
    camera = np.array(
        [[float(width), 0, width / 2.0], [0, float(width), height / 2.0], [0, 0, 1]],
        dtype=np.float64,
    )
    no_distortion = np.zeros((4, 1))

    ok, rotation, _ = cv2.solvePnP(
        _MODEL_POINTS, points, camera, no_distortion, flags=cv2.SOLVEPNP_EPNP
    )
    if not ok:
        return None

    matrix, _ = cv2.Rodrigues(rotation)
    angles = cv2.RQDecomp3x3(matrix)[0]
    pitch, yaw, roll = (float(angle) for angle in angles)
    return PoseAngles(yaw=round(_wrap(yaw), 2), pitch=round(_wrap(pitch), 2), roll=round(_wrap(roll), 2))


def _wrap(angle: float) -> float:
    """Bring an angle into -180..180, so a small movement never reads as ~180 degrees."""
    angle = (angle + 180.0) % 360.0 - 180.0
    if angle > 90:
        angle -= 180
    elif angle < -90:
        angle += 180
    return angle


def resting_pose(poses: list[PoseAngles]) -> PoseAngles:
    """The person's own resting angles: the middle of the capture, movement included."""
    if not poses:
        return PoseAngles(yaw=0.0, pitch=0.0, roll=0.0)
    return PoseAngles(
        yaw=float(np.median([pose.yaw for pose in poses])),
        pitch=float(np.median([pose.pitch for pose in poses])),
        roll=float(np.median([pose.roll for pose in poses])),
    )


def direction(pose: PoseAngles, baseline: PoseAngles) -> str:
    """Which way the head has moved from its resting position.

    Only yaw is trusted: fitting a 3D head to five landmarks pins down left and
    right well (a real turn reads 30 to 55 degrees) but not up and down, where the
    same capture wanders by 20 degrees while the person is only turning. Nodding
    needs a denser landmark model (MediaPipe's 478-point mesh, for instance).
    """
    turned = pose.yaw - baseline.yaw
    if abs(turned) >= rules.TURN_DEGREES:
        return "left" if turned < 0 else "right"
    return "centre"


def is_frontal(pose: PoseAngles, baseline: PoseAngles) -> bool:
    """Close enough to the resting pose to serve as the photo we keep."""
    return abs(pose.yaw - baseline.yaw) <= rules.FRONTAL_DEGREES
