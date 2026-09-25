"""Live guidance while a document is being held up to the camera.

The page draws a rectangle, crops each frame to it and sends only that, so what
is judged here is exactly what would be captured. This answers, in a few tens of
milliseconds, whether that crop is worth keeping:

  * is there a document in view at all, rather than a face or a desk;
  * is the portrait on it visible, and large enough to say the card fills the
    rectangle rather than sitting small in the middle of it;
  * is it sharp, evenly lit, and free of the reflection that a laminated card
    throws back at a lamp.

What is deliberately not done here is reading the document. OCR takes about a
second, which is far too slow to run on every frame, so the final word belongs to
POST /v1/document/scan on the captured still. This step exists to stop people
submitting a photo that was never going to be readable, and to tell them why
while they can still fix it.
"""

import cv2
import numpy as np

from app.engines.face import detect_faces
from app.engines.image import InvalidImageError, decode_image, to_gray
from app.rules import document as rules
from app.schemas.document import DocumentFrameProbe


def probe_document_frame(data: bytes, side: str = "front") -> DocumentFrameProbe:
    """Judge one cropped camera frame and say what to fix.

    The back of a card carries no photograph, so the portrait checks apply to the
    front only. Everything else, text, focus, light and glare, is judged the same
    on both sides: a back that cannot be read is as useless as a front that
    cannot.
    """
    try:
        image = decode_image(data)
    except InvalidImageError:
        return DocumentFrameProbe(usable=False, hints=["FRAME_UNREADABLE"])

    image = _standard_size(image)
    gray = to_gray(image)

    blur = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    brightness = float(gray.mean())
    glare = float((gray > rules.GLARE_PIXEL_VALUE).mean())
    fill = _card_fill(gray)
    lines = _text_lines(gray)

    front = side != "back"
    faces = detect_faces(image) if front else []
    portrait = faces[0].height / image.shape[0] if faces else None
    sideways = _looks_sideways(image) if front and not faces else False

    hints: list[str] = []
    if lines < rules.LIVE_MIN_TEXT_LINES:
        hints.append("NO_DOCUMENT_IN_VIEW")
    if not front:
        pass                                  # no portrait to look for on a back
    elif sideways:
        hints.append("TURN_THE_CARD_UPRIGHT")
    elif not faces:
        hints.append("PORTRAIT_NOT_VISIBLE")
    elif portrait < rules.LIVE_MIN_PORTRAIT_RATIO:
        hints.append("FIT_THE_CARD_IN_THE_BOX")
    if brightness < rules.LIVE_MIN_BRIGHTNESS:
        hints.append("TOO_DARK")
    elif brightness > rules.LIVE_MAX_BRIGHTNESS:
        hints.append("TOO_BRIGHT")
    if glare > rules.LIVE_MAX_GLARE_RATIO:
        hints.append("GLARE_ON_THE_CARD")
    if blur < rules.LIVE_MIN_BLUR:
        hints.append("HOLD_STILL")

    return DocumentFrameProbe(
        usable=not hints,
        card_fill=round(fill, 3),
        text_lines=lines,
        portrait_ratio=round(portrait, 3) if portrait is not None else None,
        blur_score=round(blur, 1),
        brightness=round(brightness, 1),
        glare_ratio=round(glare, 4),
        hints=hints,
    )


def _standard_size(image: np.ndarray) -> np.ndarray:
    """One width for every frame, so a reading means the same thing whatever
    camera and rectangle the page happens to use."""
    width = rules.LIVE_ANALYSIS_WIDTH
    if image.shape[1] == width:
        return image
    height = max(1, int(width * image.shape[0] / image.shape[1]))
    return cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)


def _text_lines(gray: np.ndarray) -> int:
    """How many line-shaped blobs of printed text the frame holds.

    Characters are smeared sideways into line-shaped blobs and the blobs with the
    proportions of a printed line are counted. An identity document is covered in
    them; a face, a hand or a desk is not. This is what stops the page taking a
    picture of somebody's face and calling it a document.
    """
    binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 15, 9)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(9, gray.shape[1] // 40), 3))
    joined = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(joined, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

    lines = 0
    for contour in contours:
        _, _, width, height = cv2.boundingRect(contour)
        wide_enough = width >= gray.shape[1] * 0.06
        line_shaped = 2.5 <= width / max(height, 1) <= 60 and height >= 3
        not_a_band = height < gray.shape[0] * 0.12
        if wide_enough and line_shaped and not_a_band:
            lines += 1
    return lines


def _card_fill(gray: np.ndarray) -> float:
    """How much of the rectangle the card covers, 0 to 1.

    A card is a bright, flat, closed shape against whatever it is lying on. The
    largest such region, after the edges are closed up, is taken as the card. If
    nothing convincing is found the answer is 0, which reads as "no card here".
    """
    edges = cv2.Canny(cv2.GaussianBlur(gray, (5, 5), 0), 40, 120)
    closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return 0.0
    biggest = max(contours, key=cv2.contourArea)
    # The card's own area, not its bounding box: a hand held diagonally across
    # the frame has a large box and a small area.
    return min(1.0, float(cv2.contourArea(biggest)) / float(gray.size))


def _looks_sideways(image: np.ndarray) -> bool:
    """Is there a face that would be found if the card were turned upright?

    Detectors are trained on upright faces, so a card lying sideways reads as
    having no portrait at all. Rather than tell the person their document has no
    photograph, which is confusing and wrong, we check the two quarter turns and
    tell them to turn it.
    """
    for rotation in (cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_90_COUNTERCLOCKWISE):
        if detect_faces(cv2.rotate(image, rotation)):
            return True
    return False
