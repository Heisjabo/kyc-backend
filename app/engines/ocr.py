import threading
from dataclasses import dataclass

import cv2
import numpy as np
import pytesseract
from rapidocr import RapidOCR

MRZ_CHARSET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789<"

# Large photos slow OCR down without improving it; cap the longest side.
_OCR_MAX_SIDE = 1600


@dataclass
class OcrLine:
    text: str
    score: float


_engine: RapidOCR | None = None
# One engine shared across request threads; the lock keeps it to one image at a time.
_engine_lock = threading.Lock()


def read_document(image: np.ndarray) -> list[OcrLine]:
    """Read every line of text on a document (RapidOCR: PaddleOCR models on ONNX Runtime, CPU).

    Lines come back roughly top to bottom, left to right. Handles tilted photos.
    """
    global _engine

    height, width = image.shape[:2]
    scale = _OCR_MAX_SIDE / max(height, width)
    if scale < 1:
        image = cv2.resize(image, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA)

    with _engine_lock:
        if _engine is None:
            _engine = RapidOCR(params={"Global.log_level": "error"})
        result = _engine(image)

    if not result.txts:
        return []
    return [OcrLine(text=text, score=float(score)) for text, score in zip(result.txts, result.scores)]


def read_text(image: np.ndarray, psm: int = 6, whitelist: str | None = None) -> str:
    """Run Tesseract on an image. psm 6 treats the image as one uniform block of text.

    Used as a second reader for the MRZ, where a character whitelist helps.
    """
    config = f"--oem 1 --psm {psm}"
    if whitelist:
        config += f" -c tessedit_char_whitelist={whitelist}"
    return pytesseract.image_to_string(image, lang="eng", config=config)
