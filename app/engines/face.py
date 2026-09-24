"""Face detection (YuNet, ONNX, CPU).

YuNet returns a box, a score and five landmarks per face: right eye, left eye,
nose tip, right mouth corner, left mouth corner (right/left as seen in the image).
Head pose is derived from those landmarks in engines/pose.py.
"""

import threading
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from app.core.config import settings
from app.rules import face as rules

_DETECTOR_INPUT = (320, 320)


@dataclass
class Landmarks:
    right_eye: tuple[float, float]
    left_eye: tuple[float, float]
    nose: tuple[float, float]
    right_mouth: tuple[float, float]
    left_mouth: tuple[float, float]


@dataclass
class Face:
    x: int
    y: int
    width: int
    height: int
    score: float
    landmarks: Landmarks

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.width / 2, self.y + self.height / 2


_detector = None
_detector_lock = threading.Lock()


def _get_detector(width: int, height: int):
    global _detector
    model = Path(settings.FACE_DETECTOR_MODEL)
    if not model.exists():
        raise FileNotFoundError(f"Face detector model not found at {model}")
    if _detector is None:
        _detector = cv2.FaceDetectorYN.create(str(model), "", _DETECTOR_INPUT, rules.FACE_SCORE_THRESHOLD)
    _detector.setInputSize((width, height))
    return _detector


def detect_faces(image: np.ndarray) -> list[Face]:
    height, width = image.shape[:2]
    with _detector_lock:
        detector = _get_detector(width, height)
        _, raw = detector.detect(image)
    if raw is None:
        return []

    faces = []
    for row in raw:
        points = row[4:14].reshape(5, 2)
        faces.append(
            Face(
                x=int(row[0]),
                y=int(row[1]),
                width=int(row[2]),
                height=int(row[3]),
                score=float(row[-1]),
                landmarks=Landmarks(*[(float(px), float(py)) for px, py in points]),
            )
        )
    return sorted(faces, key=lambda face: face.width * face.height, reverse=True)


def crop_face(image: np.ndarray, face: Face, margin: float = 0.35) -> np.ndarray:
    """Cut out the face with a margin, for the portrait we hand back or compare."""
    height, width = image.shape[:2]
    pad_x, pad_y = int(face.width * margin), int(face.height * margin)
    x1, y1 = max(0, face.x - pad_x), max(0, face.y - pad_y)
    x2, y2 = min(width, face.x + face.width + pad_x), min(height, face.y + face.height + pad_y)
    return image[y1:y2, x1:x2]


def portrait(image: np.ndarray, face: Face) -> np.ndarray:
    """The picture of a face we show: a third of the face box added on every side.

    This is the crop the document portrait has always used. The still taken
    during the capture goes through the same function, so the two pictures shown
    side by side are cut the same way.
    """
    return image[
        max(0, face.y - face.height // 3): face.y + face.height + face.height // 3,
        max(0, face.x - face.width // 3): face.x + face.width + face.width // 3,
    ]
