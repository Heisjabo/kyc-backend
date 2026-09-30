"""Was a submitted image made by a generator?

One check, used by the document step and by liveness. It runs the Community
Forensics detector (engines/ai_image.py) over the images a step received and
turns the probabilities into a verdict.

How the scores become a verdict, and why:

  * Only images at least MIN_SIDE pixels on their shorter side are scored. The
    detector enlarges everything to 440 pixels, and enlargement is itself the kind
    of smoothness it looks for: real face crops of 160 to 240 pixels scored up to
    39%, and AI ones faded towards "real". Below that size the score says more
    about the size than about the image, so the image is skipped and says so.
  * A document takes the highest score of its images. Front, back and the face on
    it are separate things a forger could generate separately, so one of them
    being generated is enough.
  * Liveness takes the median of its frames. A deepfake video is generated in
    every frame; one frame of a real person scoring oddly is not a verdict.
  * At REJECT_AT or above the check fails. Between REVIEW_AT and REJECT_AT a
    document goes to review, and liveness passes with a warning (the face step is
    pass or fail, and a person should not be failed on a middling score).

What it is good at, from what we measured on public images: faces from GAN
generators (StyleGAN, "this person does not exist") score 99 to 100% and keep that
through resizing and JPEG compression; real photographs stayed under 5%. What it
is weaker at: some diffusion images, which it can miss outright, and diffusion
images that were resized or recompressed, which lose most of their score. It also
flags photos run through generative "enhance" apps, which redraw the face. So this
is one strong signal among the others (liveness, face match, document checks),
not a gate that catches everything, and the thresholds are settings to tune once
real submissions have been scored.

It scores images as they were received, never a resized or re-encoded copy, for
the same reason: compression washes out the traces it looks for.
"""

import logging
from statistics import median

import numpy as np

from app.core.config import settings
from app.engines import ai_image
from app.schemas.document import CheckResult, CheckStatus

log = logging.getLogger(__name__)

MIN_SIDE = 256
MODEL = "community-forensics-vit-s (CVPR 2025)"


def check_ai_generated(images: list[tuple[str, np.ndarray | None]], *, subject: str) -> CheckResult:
    """Score the images and judge them. `subject` is "document" or "face"."""
    reject_at = settings.AI_IMAGE_REJECT_THRESHOLD
    review_at = settings.AI_IMAGE_REVIEW_THRESHOLD
    base = {"model": MODEL, "review_at": review_at, "reject_at": reject_at}

    if not ai_image.available():
        return CheckResult(name="ai_generated", status=CheckStatus.skipped, details={**base, "reason": "model not installed"})

    scored = [(label, image) for label, image in images if image is not None and min(image.shape[:2]) >= MIN_SIDE]
    too_small = [label for label, image in images if image is not None and min(image.shape[:2]) < MIN_SIDE]
    if not scored:
        return CheckResult(
            name="ai_generated",
            status=CheckStatus.skipped,
            details={**base, "reason": "no image large enough to score", "too_small": too_small},
        )

    try:
        probabilities = ai_image.probabilities([image for _, image in scored])
    except Exception:  # noqa: BLE001 - a detector failure must not fail a person
        log.exception("AI image detector failed")
        return CheckResult(name="ai_generated", status=CheckStatus.skipped, details={**base, "reason": "detector error"})

    scores = {label: round(p, 4) for (label, _), p in zip(scored, probabilities)}
    score = max(probabilities) if subject == "document" else median(probabilities)
    details = {**base, "score": round(score, 4), "scores": scores, "combined": "max" if subject == "document" else "median",
               "too_small": too_small}

    if score >= reject_at:
        reason = "AI_GENERATED_DOCUMENT" if subject == "document" else "AI_GENERATED_FACE"
        return CheckResult(name="ai_generated", status=CheckStatus.failed, reasons=[reason], details=details)
    if score >= review_at:
        if subject == "document":
            return CheckResult(name="ai_generated", status=CheckStatus.review, reasons=["AI_GENERATED_SUSPECTED"], details=details)
        return CheckResult(name="ai_generated", status=CheckStatus.passed, warnings=["AI_GENERATED_SUSPECTED"], details=details)
    return CheckResult(name="ai_generated", status=CheckStatus.passed, details=details)
