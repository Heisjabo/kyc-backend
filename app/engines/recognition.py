"""Face recognition: SCRFD finds the face, ArcFace describes it, cosine compares.

This follows a working implementation of the same job (a local SCRFD + ArcFace
verification service), down to the two model files: det_10g.onnx and
w600k_r50.onnx from InsightFace's buffalo_l pack, the same SHA-256 as theirs.

The path an image takes, identical for the portrait on a document and for a frame
from the camera:

  1. SCRFD detects faces at 640x640 and returns a box and five landmarks each.
     Only the largest face is taken (max_num=1, metric="max"): on a document that
     is the portrait rather than a ghost image, in a capture it is the person at
     the camera rather than somebody behind them.
  2. face_align.norm_crop warps those five landmarks onto ArcFace's canonical
     112x112 template, so both images arrive at the model framed the same way.
  3. ArcFace returns 512 numbers, which are divided by their own length. Because
     both vectors are then unit length, the cosine of the angle between them is
     their dot product, and that is the match score.

Detection for liveness stays with YuNet (engines/face.py): it feeds head pose and
quality, which are a different job. The matcher runs its own detection so that
the two images it compares are prepared identically, which is the whole point.
"""

import threading
from pathlib import Path

import numpy as np

from app.core.config import settings
from app.rules import face as rules

EMBEDDING_DIMENSION = 512

_models = None
_lock = threading.Lock()


class RecognitionUnavailableError(RuntimeError):
    """The models could not be loaded. Matching cannot run at all."""


def _session_options(ort):
    """One inference thread per session: the service is concurrent at the request
    level, and letting every session spawn threads of its own fights with that."""
    options = ort.SessionOptions()
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.intra_op_num_threads = 1
    options.enable_cpu_mem_arena = True
    options.enable_mem_pattern = True
    return options


def _providers(ort) -> list:
    """CUDA where there is a GPU, CPU otherwise. Nothing leaves the machine."""
    available = set(ort.get_available_providers())
    if settings.FACE_MODEL_DEVICE_ID >= 0 and "CUDAExecutionProvider" in available:
        return [
            ("CUDAExecutionProvider", {"device_id": settings.FACE_MODEL_DEVICE_ID}),
            "CPUExecutionProvider",
        ]
    return ["CPUExecutionProvider"]


def _load():
    """The detector and the recogniser, loaded once (about 190 MB of weights)."""
    global _models
    if _models is not None:
        return _models

    import onnxruntime as ort
    from insightface.model_zoo.arcface_onnx import ArcFaceONNX
    from insightface.model_zoo.scrfd import SCRFD

    detection = Path(settings.FACE_DETECTION_MODEL)
    recognition = Path(settings.FACE_RECOGNITION_MODEL)
    for model in (detection, recognition):
        if not model.is_file():
            raise RecognitionUnavailableError(f"Face model not found at {model}. See README.")

    options, providers = _session_options(ort), _providers(ort)
    on_gpu = any("CUDAExecutionProvider" in str(provider) for provider in providers)
    ctx = settings.FACE_MODEL_DEVICE_ID if on_gpu else -1

    detector = SCRFD(
        model_file=str(detection),
        session=ort.InferenceSession(str(detection), sess_options=options, providers=providers),
    )
    recogniser = ArcFaceONNX(
        model_file=str(recognition),
        session=ort.InferenceSession(str(recognition), sess_options=options, providers=providers),
    )
    detector.prepare(ctx_id=ctx, input_size=rules.DETECT_SIZE, det_thresh=rules.DETECT_THRESHOLD)
    recogniser.prepare(ctx_id=ctx)

    if not detector.use_kps:
        # Without the five landmarks there is no alignment, and without alignment
        # the model is comparing framings as much as faces.
        raise RecognitionUnavailableError("The detection model does not return facial landmarks")

    _models = (detector, recogniser)
    return _models


def find_face(image: np.ndarray):
    """The largest face SCRFD sees, as (box, landmarks), or None.

    The same detector the match uses, so anything decided from it agrees with
    what the comparison will actually be able to do.
    """
    with _lock:
        detector, _ = _load()
        boxes, landmarks = detector.detect(image, max_num=1, metric="max")
    if boxes.shape[0] == 0 or landmarks is None or landmarks.shape[0] == 0:
        return None
    return boxes[0], landmarks[0]


def embed(image: np.ndarray) -> np.ndarray | None:
    """The 512-number vector for the largest face in the image, or None."""
    with _lock:
        detector, recogniser = _load()
        boxes, landmarks = detector.detect(image, max_num=1, metric="max")
        if boxes.shape[0] == 0 or landmarks is None or landmarks.shape[0] == 0:
            return None

        from insightface.utils import face_align

        aligned = face_align.norm_crop(
            image, landmark=landmarks[0], image_size=recogniser.input_size[0]
        )
        vector = np.asarray(recogniser.get_feat(aligned), dtype=np.float32).reshape(-1)

    if vector.size != EMBEDDING_DIMENSION or not np.isfinite(vector).all():
        return None
    length = float(np.linalg.norm(vector))
    if length <= np.finfo(np.float32).eps:
        return None
    return np.ascontiguousarray(vector / length, dtype=np.float32)


def similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity, -1 to 1. Higher means more alike.

    Each vector is scaled by its largest element before the norm is taken, which
    keeps the arithmetic away from overflow and underflow, and the result is
    clipped: floating point can land a few ulps outside the range.
    """
    left, right = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
    if left.shape != right.shape or left.size == 0:
        return 0.0
    if not (np.isfinite(left).all() and np.isfinite(right).all()):
        return 0.0
    try:
        return float(np.clip(np.dot(_unit(left), _unit(right)), -1.0, 1.0))
    except ValueError:
        return 0.0


def _unit(vector: np.ndarray) -> np.ndarray:
    scale = float(np.max(np.abs(vector)))
    if scale == 0.0:
        raise ValueError("a zero vector has no direction")
    scaled = vector / scale
    return scaled / float(np.sqrt(np.dot(scaled, scaled)))


def pass_threshold() -> float:
    """The similarity at or above which two faces are treated as one person.

    Kept in rules/face.py with the measurements behind it. InsightFace publishes
    no threshold for this pack: the right line depends on how the two images were
    taken, and ours are a photograph printed on a card against a webcam frame.
    """
    return rules.MATCH_THRESHOLD


def warm_up() -> None:
    """Load the models at start-up so the first real request is not slow."""
    with _lock:
        _load()
