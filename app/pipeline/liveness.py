"""Liveness: deciding whether the frames came from a person in front of the camera.

Four things have to hold:

  1. every frame holds exactly one face, and the same one throughout;
  2. the movements asked for actually happened (nod up and down, turn left and
     right), each held rather than glimpsed;
  3. the thing that moved has depth: a printed photo or a phone screen is flat,
     so a flat-surface transform explains its movement almost perfectly, and a
     face's does not;
  4. one frontal, sharp frame is left over to serve as the photo.

A head-turn challenge alone is not enough: a print tilted at the camera produces
the same landmark movement as a turn, which is why (3) is here.
"""

import base64
import time

import cv2
import numpy as np

from app.engines.face import Face, crop_face, detect_faces, portrait
from app.engines.image import InvalidImageError, decode_image, to_gray
from app.engines.mesh import Mesh, face_mesh
from app.engines.signals import CaptureSignals, read_capture
from app.pipeline.ai_image import check_ai_generated
from app.rules import face as rules
from app.schemas.document import CheckResult, CheckStatus, Decision
from app.schemas.face import CapturedFace, ChallengeProgress, FramePose, LivenessResponse


def check_liveness(
    frames: list[bytes],
    challenge_type: str = rules.DEFAULT_CHALLENGE,
    resting: Mesh | None = None,
    claims: dict[int, str] | None = None,
) -> LivenessResponse:
    started = time.perf_counter()
    required = rules.CHALLENGES[challenge_type]
    checks: list[CheckResult] = []

    images: list[np.ndarray | None] = []
    faces: list[Face | None] = []
    meshes: list[Mesh | None] = []
    crowded: list[int] = []
    too_small: list[int] = []

    # 1. Read every frame: one face, and its mesh.
    for index, data in enumerate(frames):
        try:
            image = decode_image(data)
        except InvalidImageError:
            images.append(None)
            faces.append(None)
            meshes.append(None)
            continue

        images.append(image)
        found = detect_faces(image)
        if len(found) > 1:
            crowded.append(index)
        face = found[0] if len(found) == 1 else None
        if face is not None and _too_small(face, image):
            too_small.append(index)
            face = None
        faces.append(face)
        meshes.append(face_mesh(image, face) if face is not None else None)

    usable = [index for index, mesh in enumerate(meshes) if mesh is not None]
    checks.append(_face_presence_check(frames, usable, crowded, too_small))
    checks.append(_stability_check(images, faces, usable))

    if not usable:
        return _finish(checks, required, CaptureSignals(), None, frames, 0, started, challenge_type)

    # 2. Everything is measured against the person's own resting pose. It comes
    #    from the caller when the capture was guided, because that is the mesh
    #    the guidance used: measuring against a different one here would let the
    #    server disagree with the answers it gave the person a second earlier,
    #    and refuse movements they were told they had completed.
    if resting is None:
        resting = meshes[usable[0]]
    capture = read_capture(meshes, resting, claims)

    checks.append(_challenge_check(capture, required))
    checks.append(_depth_check(capture))

    still = _best_capture(images, faces, capture, usable)
    checks.append(
        CheckResult(
            name="capture",
            status=CheckStatus.passed if still else CheckStatus.failed,
            reasons=[] if still else ["NO_USABLE_CAPTURE"],
            details={"frame_index": still.frame_index if still else None},
        )
    )
    # 5. Were the frames made by a generator (an injected deepfake stream)?
    checks.append(check_ai_generated(_ai_frames(images, usable, still), subject="face"))
    return _finish(checks, required, capture, still, frames, len(usable), started, challenge_type)


def _finish(checks, required, capture, still, frames, with_face, started, challenge_type) -> LivenessResponse:
    seen = [step for step in required if step in capture.steps_seen]
    missing = [step for step in required if step not in seen]
    return LivenessResponse(
        decision=_decide(checks),
        reasons=_unique(reason for check in checks for reason in check.reasons),
        warnings=_unique(warning for check in checks for warning in check.warnings),
        challenge=ChallengeProgress(type=challenge_type, required=required, completed=seen, missing=missing),
        baseline_yaw=0.0,
        baseline_pitch=0.0,
        planarity=capture.planarity,
        frames_received=len(frames),
        frames_with_face=with_face,
        poses=[
            FramePose(
                index=frame.index,
                faces=1,
                yaw=frame.yaw,
                pitch=frame.pitch,
                direction=frame.direction,
                eye_aspect_ratio=frame.eye_aspect_ratio,
            )
            for frame in capture.frames
        ],
        capture=still,
        checks=checks,
        processing_ms=int((time.perf_counter() - started) * 1000),
    )


def _ai_frames(images, usable: list[int], still: CapturedFace | None) -> list[tuple[str, np.ndarray]]:
    """The still, and two frames spread through the capture: three looks at it.

    Whole frames as received, not the portrait crop: the crop is re-encoded, and
    compression washes out what the detector looks for.
    """
    picks = [still.frame_index] if still else []
    for index in (usable[len(usable) // 3], usable[2 * len(usable) // 3]):
        if index not in picks:
            picks.append(index)
    return [(f"frame_{index}", images[index]) for index in picks[:3] if images[index] is not None]


def _unique(items) -> list:
    return list(dict.fromkeys(item for item in items if item))


def _decide(checks: list[CheckResult]) -> Decision:
    if any(check.status == CheckStatus.failed for check in checks):
        return Decision.retake
    if any(check.status == CheckStatus.review for check in checks):
        return Decision.review
    return Decision.passed


def _too_small(face: Face, image: np.ndarray) -> bool:
    return face.height < min(image.shape[:2]) * rules.MIN_FACE_RATIO


def _face_presence_check(frames, usable, crowded, too_small) -> CheckResult:
    ratio = len(usable) / len(frames) if frames else 0
    reasons, warnings = [], []
    if too_small:
        warnings.append("FACE_TOO_FAR_FROM_CAMERA")
    if crowded:
        reasons.append("MULTIPLE_FACES_DETECTED")
    if not usable:
        reasons.append("NO_FACE_DETECTED")
    elif ratio < rules.MIN_FACE_FRAME_RATIO:
        reasons.append("FACE_LOST_DURING_CHALLENGE")
    return CheckResult(
        name="face_presence",
        status=CheckStatus.failed if reasons else CheckStatus.passed,
        reasons=reasons,
        warnings=warnings,
        details={"frames_with_one_face": len(usable), "frames_with_several": crowded},
    )


def _stability_check(images, faces, usable) -> CheckResult:
    """The face should move smoothly; jumps mean separate photos, not one capture."""
    if len(usable) < 2:
        return CheckResult(name="face_stability", status=CheckStatus.skipped)

    jumps = []
    for previous, current in zip(usable, usable[1:]):
        before, after = faces[previous], faces[current]
        if before is None or after is None:
            continue
        width = images[current].shape[1]
        moved = float(np.hypot(after.center[0] - before.center[0], after.center[1] - before.center[1]) / width)
        resized = abs(after.height - before.height) / max(before.height, 1)
        if moved > rules.MAX_CENTRE_JUMP_RATIO or resized > rules.MAX_SIZE_CHANGE_RATIO:
            jumps.append({"from": previous, "to": current, "moved": round(moved, 3)})

    return CheckResult(
        name="face_stability",
        status=CheckStatus.review if jumps else CheckStatus.passed,
        reasons=["FACE_UNSTABLE_BETWEEN_FRAMES"] if jumps else [],
        details={"jumps": jumps},
    )


def _challenge_check(capture: CaptureSignals, required: list[str]) -> CheckResult:
    missing = [step for step in required if step not in capture.steps_seen]
    ok = len(missing) <= rules.ALLOWED_MISSING_DIRECTIONS
    return CheckResult(
        name="challenge",
        status=CheckStatus.passed if ok else CheckStatus.failed,
        reasons=[] if ok else ["CHALLENGE_NOT_COMPLETED"],
        details={"required": required, "seen": capture.steps_seen, "missing": missing},
    )


def _depth_check(capture: CaptureSignals) -> CheckResult:
    """Did the thing that moved have depth, or was it flat?"""
    if capture.planarity is None:
        return CheckResult(
            name="depth",
            status=CheckStatus.failed,
            reasons=["DEPTH_NOT_MEASURED"],
            details={"turn_degrees": capture.turn_degrees, "needed": rules.TURN_DEGREES},
        )
    flat = capture.planarity < rules.PLANARITY_MIN
    return CheckResult(
        name="depth",
        status=CheckStatus.failed if flat else CheckStatus.passed,
        reasons=["FLAT_SURFACE_DETECTED"] if flat else [],
        details={"planarity": capture.planarity, "minimum": rules.PLANARITY_MIN},
    )


def _best_capture(images, faces, capture: CaptureSignals, usable) -> CapturedFace | None:
    """The sharpest frame where the person faced the camera with their eyes open."""
    best, best_score = None, -1.0
    for frame in capture.frames:
        if frame.direction != "centre" or frame.eyes_closed or abs(frame.yaw) > rules.FRONTAL_DEGREES:
            continue
        image, face = images[frame.index], faces[frame.index]
        if image is None or face is None:
            continue
        measured = crop_face(image, face)
        if measured.size == 0:
            continue
        sharpness = float(cv2.Laplacian(to_gray(measured), cv2.CV_64F).var())
        if sharpness > best_score:
            best, best_score = (frame.index, image, face), sharpness

    if best is None:
        return None
    index, image, face = best
    # The same crop as the document portrait, so the two pictures put side by
    # side are cut the same way.
    crop = portrait(image, face)
    ok, buffer = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        return None
    return CapturedFace(
        frame_index=index,
        blur_score=round(best_score, 2),
        width=crop.shape[1],
        height=crop.shape[0],
        image_base64=base64.b64encode(buffer.tobytes()).decode(),
    )
