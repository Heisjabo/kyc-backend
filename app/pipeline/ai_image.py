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
  * **It never fails a verification.** At DETECT_AT or above the check passes and
    warns, and the step records that an image was generated. A detector that
    refuses people would refuse real ones: it misses some diffusion images and
    flags photographs put through a generative "enhance" app, which plenty of
    phones now do by default. So the other checks decide the verdict, and this
    rides along as a flag a person can act on. Between SUSPECT_AT and DETECT_AT it
    warns more softly and the flag stays off.

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

# The warning a step carries when an image scored at or above the detect
# threshold. One code for every subject: the details say which image it was.
AI_GENERATED = "AI_GENERATED_IMAGE"
AI_SUSPECTED = "AI_GENERATED_SUSPECTED"


def detected(check: CheckResult) -> bool | None:
    """Did this check find a generated image? None when it could not look.

    What goes in verification_steps.ai_generated: true, false, or null for a step
    whose images were too small to score, or that ran without the model installed.
    """
    if check.status == CheckStatus.skipped:
        return None
    return AI_GENERATED in check.warnings


def score_of(check: CheckResult) -> float | None:
    return check.details.get("score")


def check_ai_generated(images: list[tuple[str, np.ndarray | None]], *, subject: str) -> CheckResult:
    """Score the images and report. `subject` is "document", "face" or "address"."""
    detect_at = settings.AI_IMAGE_DETECT_THRESHOLD
    suspect_at = settings.AI_IMAGE_SUSPECT_THRESHOLD
    base = {"model": MODEL, "suspect_at": suspect_at, "detect_at": detect_at}

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
    # A document's images are separate things a forger could generate separately,
    # so the highest counts. A face check's frames are one stream, and a generated
    # stream is generated in every frame, so the median counts and one odd frame is
    # not a verdict.
    combined = "median" if subject == "face" else "max"
    score = median(probabilities) if combined == "median" else max(probabilities)
    details = {**base, "score": round(score, 4), "scores": scores, "combined": combined,
               "too_small": too_small, "subject": subject}

    # Always passed. The warning is the output, and the step carries the flag.
    if score >= detect_at:
        return CheckResult(name="ai_generated", status=CheckStatus.passed,
                           warnings=[AI_GENERATED], details=details)
    if score >= suspect_at:
        return CheckResult(name="ai_generated", status=CheckStatus.passed,
                           warnings=[AI_SUSPECTED], details=details)
    return CheckResult(name="ai_generated", status=CheckStatus.passed, details=details)
