"""Face recognition with DeepFace: turn a face into an embedding and compare two.

Detection stays with YuNet (one detector for the whole service), so DeepFace is
given a face this module has already cut out and levelled, with its own detection
skipped.

The pass threshold is not ours: it comes from DeepFace's own tuned table, which
holds one value per model and distance metric (deepface/modules/verification.py,
find_threshold). We ask it for the configured model rather than hardcoding a
number, because the scales differ wildly between models: a cosine distance of
0.30 for Facenet512, 0.593 for SFace, 0.68 for ArcFace.
"""

import os
import threading
from functools import lru_cache

import cv2
import numpy as np

os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")  # keep TensorFlow's banner out of the logs
# DeepFace prints a deprecation banner about backend installation on every import.
# We pin the backend in requirements.txt (deepface[tensorflow]), so the notice is
# noise: ERROR and above still come through.
os.environ.setdefault("DEEPFACE_LOG_LEVEL", "40")

from app.core.config import settings
from app.engines.face import Face, detect_faces, portrait
from app.rules import face as rules

_lock = threading.Lock()
_warmed = False


def _deepface():
    """Imported lazily: loading the backend takes seconds and most requests never need it."""
    from deepface import DeepFace

    return DeepFace


def align(image: np.ndarray, face: Face) -> np.ndarray:
    """Prepare the face for the model: the same crop used everywhere else.

    One crop rule in the whole service, the one the document portrait has always
    used (a third of the face box added on every side), resized to the model's
    input size. Nothing else is done to the face.
    """
    crop = portrait(image, face)
    if crop.size == 0:
        crop = image
    return cv2.resize(crop, rules.ALIGN_SIZE, interpolation=cv2.INTER_CUBIC)


def embed(image: np.ndarray, face: Face | None = None) -> np.ndarray | None:
    """Embedding of the largest face in the image, or None if there is no face."""
    if face is None:
        found = detect_faces(image)
        if not found:
            return None
        face = found[0]

    prepared = align(image, face)
    with _lock:
        results = _deepface().represent(
            img_path=prepared,
            model_name=settings.FACE_RECOGNITION_MODEL,
            detector_backend="skip",  # YuNet already found and levelled the face
            enforce_detection=False,
            normalization="base",
        )
    if not results:
        return None
    return np.asarray(results[0]["embedding"], dtype=np.float32)


@lru_cache(maxsize=8)
def pass_threshold(model: str | None = None) -> float:
    """DeepFace's tuned same-person line for this model, as a similarity.

    DeepFace states it as a cosine *distance*; similarity is 1 minus that.
    """
    from deepface.modules.verification import find_threshold

    return 1.0 - find_threshold(model or settings.FACE_RECOGNITION_MODEL, "cosine")


def confidence(score: float, model: str | None = None) -> float:
    """DeepFace's calibrated confidence for a score, as a percentage.

    DeepFace fits a logistic regression per model that turns a distance into the
    probability that the classification is right. Same-person answers land in
    51-100%, different-person answers in 0-49%, so the number jumps at the
    threshold: it is confidence in the verdict, not a "how similar" percentage.
    """
    from deepface.modules.verification import find_confidence

    name = model or settings.FACE_RECOGNITION_MODEL
    distance = 1.0 - score
    return round(find_confidence(distance, name, distance <= (1.0 - pass_threshold(name)), "cosine"), 1)


def similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity, -1 to 1. Higher means more alike."""
    norm = float(np.linalg.norm(a) * np.linalg.norm(b))
    if norm == 0:
        return 0.0
    return float(np.dot(a, b) / norm)


def warm_up() -> None:
    """Load the model once at start-up so the first real request isn't slow."""
    global _warmed
    if _warmed:
        return
    blank = np.zeros((rules.ALIGN_SIZE[1], rules.ALIGN_SIZE[0], 3), dtype=np.uint8)
    with _lock:
        _deepface().represent(
            img_path=blank,
            model_name=settings.FACE_RECOGNITION_MODEL,
            detector_backend="skip",
            enforce_detection=False,
        )
    _warmed = True
