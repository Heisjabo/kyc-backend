from dataclasses import dataclass, field

import cv2
import numpy as np

from app.engines.image import resize_to_width, to_gray
from app.rules import document as rules


@dataclass
class QualityReport:
    width: int
    height: int
    blur_score: float
    brightness: float
    glare_ratio: float
    issues: list[str] = field(default_factory=list)


def assess_quality(image: np.ndarray) -> QualityReport:
    height, width = image.shape[:2]

    # Measure on a fixed width so scores are comparable across camera resolutions.
    gray = to_gray(resize_to_width(image, rules.QUALITY_ANALYSIS_WIDTH))

    # Variance of the Laplacian: sharp edges give high variance, blur flattens it.
    blur_score = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    brightness = float(gray.mean())
    glare_ratio = _glare_ratio(gray)

    issues = []
    if min(width, height) < rules.MIN_SHORT_SIDE_PX or max(width, height) < rules.MIN_LONG_SIDE_PX:
        issues.append("IMAGE_LOW_RESOLUTION")
    if blur_score < rules.MIN_BLUR_SCORE:
        issues.append("IMAGE_BLURRY")
    if brightness < rules.MIN_BRIGHTNESS:
        issues.append("IMAGE_TOO_DARK")
    if brightness > rules.MAX_BRIGHTNESS:
        issues.append("IMAGE_TOO_BRIGHT")
    if glare_ratio > rules.MAX_GLARE_RATIO:
        issues.append("IMAGE_GLARE")

    return QualityReport(
        width=width,
        height=height,
        blur_score=round(blur_score, 2),
        brightness=round(brightness, 2),
        glare_ratio=round(glare_ratio, 4),
        issues=issues,
    )


def _glare_ratio(gray: np.ndarray) -> float:
    """Size of the largest blown-out spot inside the image, as a share of the image.

    Bright regions that touch the edge are background or white paper, not glare,
    so only enclosed spots count (a flash reflection on the card surface).
    """
    saturated = (gray >= rules.GLARE_PIXEL_VALUE).astype(np.uint8)
    count, _, stats, _ = cv2.connectedComponentsWithStats(saturated, connectivity=8)
    height, width = gray.shape
    largest = 0
    for x, y, w, h, area in stats[1:count]:
        touches_edge = x == 0 or y == 0 or x + w >= width or y + h >= height
        if not touches_edge:
            largest = max(largest, int(area))
    return largest / gray.size
