"""AI-generated image detection (Community Forensics ViT-S, ONNX, CPU).

The model is the detector from "Community Forensics: Using Thousands of Generators
to Train Fake Image Detectors" (Park and Owens, CVPR 2025), MIT licensed. It was
trained on 2.7 million images from 4,803 generators, most of them text-to-image
diffusion models, which is what makes it useful here: a forger does not tell us
which generator they used, and a detector trained on a handful of them only knows
those.

The file is the author's corrected export (onnx/model.onnx at a pinned revision,
see README). The older export that most search results point to, including the
onnx-community mirror, was built from the wrong weights with an untrained
two-class head, and scores noise.

It answers one question about a whole image: was this made by a generator? One
logit comes out; its sigmoid is the probability the image is AI-generated.

Preprocessing matches training exactly, because a detector of generator
fingerprints is sensitive to how pixels are resampled: the shortest edge is
resized to 440 with bicubic interpolation (aspect ratio kept), the centre 384x384
is cropped, and the result is normalised with CLIP's mean and standard deviation.

The model is optional. When the file is not there, availability is false and the
check that uses it reports itself as skipped rather than failing everyone.
"""

import threading
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from app.core.config import settings
# One policy for every ONNX model in the service: one inference thread per
# session, CUDA only when a GPU index is configured.
from app.engines.recognition import _providers, _session_options

_RESIZE_SHORTEST = 440
_CROP = 384
_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
_STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)

_session = None
_lock = threading.Lock()


def available() -> bool:
    """Whether the model file is installed. The check is skipped without it."""
    return Path(settings.AI_IMAGE_MODEL).is_file()


def _load():
    global _session
    if _session is not None:
        return _session
    with _lock:
        if _session is None:
            import onnxruntime as ort

            _session = ort.InferenceSession(
                str(Path(settings.AI_IMAGE_MODEL)),
                sess_options=_session_options(ort),
                providers=_providers(ort),
            )
    return _session


def _prepare(image: np.ndarray) -> np.ndarray:
    """A BGR image from OpenCV, as the (3, 384, 384) tensor the model was trained on."""
    rgb = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
    width, height = rgb.size
    scale = _RESIZE_SHORTEST / min(width, height)
    resized = rgb.resize(
        (max(_CROP, round(width * scale)), max(_CROP, round(height * scale))),
        Image.Resampling.BICUBIC,
    )
    left = (resized.width - _CROP) // 2
    top = (resized.height - _CROP) // 2
    crop = resized.crop((left, top, left + _CROP, top + _CROP))
    pixels = np.asarray(crop, dtype=np.float32) / 255.0
    return ((pixels - _MEAN) / _STD).transpose(2, 0, 1)


def probabilities(images: list[np.ndarray]) -> list[float]:
    """The probability each image is AI-generated, from 0 to 1, in order.

    Images too small to say anything about (under 64 pixels on a side) score
    nothing and are left out by the caller.
    """
    if not images:
        return []
    session = _load()
    batch = np.stack([_prepare(image) for image in images]).astype(np.float32)
    logits = session.run(None, {session.get_inputs()[0].name: batch})[0].reshape(-1)
    return [float(1.0 / (1.0 + np.exp(-value))) for value in logits]
