import io

import cv2
import numpy as np

from app.core.config import settings
from PIL import Image, ImageOps, UnidentifiedImageError


class InvalidImageError(ValueError):
    pass


def decode_image(data: bytes) -> np.ndarray:
    """Decode uploaded bytes into an OpenCV BGR image, honouring EXIF rotation."""
    try:
        with Image.open(io.BytesIO(data)) as pil_image:
            # The header says how big it will be before anything is decoded. Refuse a
            # picture that would take gigabytes of memory to open.
            width, height = pil_image.size
            if max(width, height) > settings.MAX_IMAGE_SIDE_PX or width * height > settings.MAX_IMAGE_PIXELS:
                raise InvalidImageError("The image is too large; send a smaller photo")
            # Phone photos are often stored sideways with an EXIF rotation flag.
            pil_image = ImageOps.exif_transpose(pil_image)
            if pil_image.mode in ("RGBA", "LA", "P"):
                # Transparent areas have no real colour; put them on white like paper.
                pil_image = pil_image.convert("RGBA")
                background = Image.new("RGBA", pil_image.size, (255, 255, 255, 255))
                pil_image = Image.alpha_composite(background, pil_image)
            rgb = pil_image.convert("RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise InvalidImageError("File is not a readable image") from exc

    return cv2.cvtColor(np.asarray(rgb), cv2.COLOR_RGB2BGR)


def to_gray(image: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def deskew(gray: np.ndarray, max_angle: float = 15.0) -> np.ndarray:
    """Straighten a slightly tilted document using the angle of its text lines.

    Characters are smeared horizontally into line-shaped blobs; the median tilt
    of the wide blobs is the page tilt. OCR degrades quickly beyond ~2 degrees.
    """
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(15, gray.shape[1] // 60), 3))
    blobs = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(blobs, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

    angles = []
    for contour in contours:
        (_, _), (w, h), angle = cv2.minAreaRect(contour)
        if w < h:
            w, h = h, w
            angle += 90
        if w > gray.shape[1] * 0.25 and w > 5 * h:
            angles.append(angle)
    if not angles:
        return gray

    angle = float(np.median(angles))
    if abs(angle) < 0.3 or abs(angle) > max_angle:
        return gray

    height, width = gray.shape
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1.0)
    return cv2.warpAffine(gray, matrix, (width, height), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def resize_to_width(image: np.ndarray, width: int) -> np.ndarray:
    height = int(image.shape[0] * width / image.shape[1])
    interpolation = cv2.INTER_CUBIC if width > image.shape[1] else cv2.INTER_AREA
    return cv2.resize(image, (width, height), interpolation=interpolation)
