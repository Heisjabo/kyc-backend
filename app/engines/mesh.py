"""The 478-point face mesh, and the liveness signals that need it.

Five landmarks are enough to find and align a face, but not to tell a person from
a photograph of one. This runs MediaPipe's face landmark model, which returns 478
points with a depth value each, and derives from it:

  * head pose, by fitting the whole mesh rather than four points
  * eye aspect ratio, so a still with shut eyes is not kept as the photo
  * planarity, the share of the mesh that a flat-surface transform fails to
    explain: a sheet of paper or a phone screen moves by a homography, a face
    does not. This is the "3D structure from landmarks" test in the face
    anti-spoofing literature, where a lack of surface variation is evidence that
    the source is 2D.

The model is Google's, Apache 2.0, and is run through the TFLite interpreter
rather than the MediaPipe graph, which crashes on macOS.
"""

import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from app.core.config import settings
from app.engines.face import Face
from app.rules import face as rules

_MESH_INPUT = 256
# Points around each eye: two above the pupil, two below, and the two corners.
_LEFT_EYE = ((159, 158), (145, 153), (33, 133))
_RIGHT_EYE = ((386, 385), (374, 380), (362, 263))

_interpreter = None
_lock = threading.Lock()


@dataclass
class Mesh:
    """478 points in the face's own crop space: x, y in pixels, z as relative depth."""

    points: np.ndarray
    presence: float

    @property
    def eye_aspect_ratio(self) -> float:
        """How open the eyes are: eyelid gap over eye width, averaged over both.

        Around 0.25 to 0.30 open, near 0 closed. The blink literature counts a
        blink when it drops below roughly 0.21 to 0.25 for a few frames running.
        """
        return float(np.mean([self._ear(eye) for eye in (_LEFT_EYE, _RIGHT_EYE)]))

    def _ear(self, eye) -> float:
        top, bottom, corners = eye
        width = np.linalg.norm(self.points[corners[0], :2] - self.points[corners[1], :2]) or 1.0
        lids = [
            np.linalg.norm(self.points[t, :2] - self.points[b, :2])
            for t, b in zip(top, bottom)
        ]
        return float(np.mean(lids) / width)


def _load():
    global _interpreter
    if _interpreter is None:
        from ai_edge_litert.interpreter import Interpreter

        bundle = Path(settings.FACE_MESH_MODEL)
        if not bundle.exists():
            raise FileNotFoundError(f"Face mesh model not found at {bundle}")
        with zipfile.ZipFile(bundle) as archive:
            model = archive.read("face_landmarks_detector.tflite")
        _interpreter = Interpreter(model_content=model)
        _interpreter.allocate_tensors()
    return _interpreter


def _square_crop(image: np.ndarray, face: Face) -> np.ndarray:
    """A square crop around the face: a squashed crop distorts the mesh."""
    centre_x, centre_y = face.center
    side = int(max(face.width, face.height) * (1 + 2 * rules.MESH_CROP_MARGIN))
    x, y = int(centre_x - side / 2), int(centre_y - side / 2)
    pad = max(0, -x, -y, x + side - image.shape[1], y + side - image.shape[0])
    if pad:
        image = cv2.copyMakeBorder(image, pad, pad, pad, pad, cv2.BORDER_REPLICATE)
        x, y = x + pad, y + pad
    return image[y:y + side, x:x + side]


def face_mesh(image: np.ndarray, face: Face) -> Mesh | None:
    """The 478-point mesh for this face, or None if the model cannot find one."""
    crop = _square_crop(image, face)
    if crop.size == 0:
        return None

    prepared = cv2.resize(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB), (_MESH_INPUT, _MESH_INPUT))
    prepared = prepared.astype(np.float32) / 255.0

    with _lock:
        interpreter = _load()
        inputs = interpreter.get_input_details()
        outputs = interpreter.get_output_details()
        interpreter.set_tensor(inputs[0]["index"], prepared[None])
        interpreter.invoke()
        points = interpreter.get_tensor(outputs[0]["index"]).reshape(478, 3).copy()
        presence = float(interpreter.get_tensor(outputs[1]["index"]).reshape(-1)[0])

    return Mesh(points=points, presence=presence)


def head_rotation(reference: Mesh, current: Mesh) -> tuple[float, float, float]:
    """Yaw, pitch and roll in degrees, from the rotation that maps one mesh onto
    the other (Kabsch). Measured against the person's own resting mesh, so face
    shape and camera position cancel out."""
    a = reference.points - reference.points.mean(0)
    b = current.points - current.points.mean(0)
    a = a / (np.linalg.norm(a) or 1.0)
    b = b / (np.linalg.norm(b) or 1.0)

    u, _, vt = np.linalg.svd(a.T @ b)
    flip = np.sign(np.linalg.det(vt.T @ u.T))
    rotation = vt.T @ np.diag([1.0, 1.0, flip]) @ u.T

    yaw = np.degrees(np.arctan2(-rotation[2, 0], np.hypot(rotation[0, 0], rotation[1, 0])))
    pitch = np.degrees(np.arctan2(rotation[2, 1], rotation[2, 2]))
    roll = np.degrees(np.arctan2(rotation[1, 0], rotation[0, 0]))
    return round(float(yaw), 2), round(float(pitch), 2), round(float(roll), 2)


def planarity(reference: Mesh, current: Mesh) -> float | None:
    """How badly a flat-surface transform fails to explain the movement, as a
    percentage of face size.

    Fits the homography that best maps one set of points onto the other. A photo
    is flat, so a homography explains it almost perfectly and the error is near
    zero. A real face is not flat, so the error is several times larger.
    """
    source = np.ascontiguousarray(reference.points[:, :2], dtype=np.float32)
    target = np.ascontiguousarray(current.points[:, :2], dtype=np.float32)

    matrix, _ = cv2.findHomography(source, target, cv2.RANSAC, 3.0)
    if matrix is None:
        return None

    projected = cv2.perspectiveTransform(source.reshape(-1, 1, 2), matrix).reshape(-1, 2)
    face_size = float(np.linalg.norm(target.max(0) - target.min(0))) or 1.0
    error = float(np.median(np.linalg.norm(projected - target, axis=1)))
    return round(error / face_size * 100, 3)
