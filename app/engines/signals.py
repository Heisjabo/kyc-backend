"""Reading a capture: what the head did, and whether the thing in front of the
camera has depth.

Everything here works on a sequence of meshes, one per frame, measured against
the person's own resting mesh so that face shape and camera position cancel out.
"""

from dataclasses import dataclass, field

from app.engines.mesh import Mesh, head_rotation, planarity
from app.rules import face as rules


@dataclass
class FrameSignals:
    index: int
    yaw: float
    pitch: float
    roll: float
    eye_aspect_ratio: float
    # "left", "right", "up", "down" or "centre", against the resting pose.
    direction: str
    eyes_closed: bool
    planarity: float | None = None


@dataclass
class CaptureSignals:
    frames: list[FrameSignals] = field(default_factory=list)
    steps_seen: list[str] = field(default_factory=list)
    # The worst-case flatness: how much a flat surface fails to explain the
    # largest movement in the capture. Low means a photograph.
    planarity: float | None = None
    turn_degrees: float = 0.0
    # Where this person's eyes counted as shut, for choosing the still.
    closed_line: float = rules.EYES_OPEN_EAR


def direction_of(yaw: float, pitch: float) -> str:
    """Which way the head moved. Turning moves it much further than nodding, so
    yaw is checked first."""
    if abs(yaw) >= rules.TURN_DEGREES:
        return "left" if yaw < 0 else "right"
    if abs(pitch) >= rules.NOD_DEGREES:
        return "up" if pitch < 0 else "down"
    return "centre"


def closed_line(meshes: list[Mesh | None]) -> float:
    """Where this person's eyes count as shut, so a still with closed eyes is not
    chosen as the photo. Relative to their own open measurement, since eye shape
    varies. Not a challenge step: a blink is too short to catch reliably in a
    browser capture."""
    opens = sorted(mesh.eye_aspect_ratio for mesh in meshes if mesh is not None)
    if not opens:
        return rules.EYES_OPEN_EAR
    typical = opens[int(len(opens) * 0.75)]       # an open-eyed frame, not an outlier
    return max(rules.EYES_OPEN_EAR, typical * rules.EYES_OPEN_RATIO)


def read_capture(
    meshes: list[Mesh | None],
    resting: Mesh,
    claims: dict[int, str] | None = None,
) -> CaptureSignals:
    """Turn a sequence of meshes into the signals the decision is made from.

    `claims` says which frame the guidance accepted for which step. Those frames
    are still measured here: a claim only says where to look. Without claims (a
    caller posting raw frames), a movement has to be held across frames instead,
    which is the only way to tell a deliberate movement from head drift.
    """
    capture = CaptureSignals()
    capture.closed_line = closed_line(meshes)
    held: str | None = None
    held_run = 0

    for index, mesh in enumerate(meshes):
        if mesh is None:
            continue

        yaw, pitch, roll = head_rotation(resting, mesh)
        ear = mesh.eye_aspect_ratio
        closed = ear < capture.closed_line

        signals = FrameSignals(
            index=index,
            yaw=yaw,
            pitch=pitch,
            roll=roll,
            eye_aspect_ratio=round(ear, 3),
            direction=direction_of(yaw, pitch),
            eyes_closed=closed,
        )
        capture.frames.append(signals)

        claimed = (claims or {}).get(index)
        if claimed is not None:
            # The guidance says this frame is the movement. Believe it only if
            # the frame really shows it.
            if signals.direction == claimed and claimed not in capture.steps_seen:
                capture.steps_seen.append(claimed)
        elif not claims:
            # Unguided capture: a direction has to be held, not glimpsed.
            held_run = held_run + 1 if signals.direction == held else 1
            held = signals.direction
            if (
                signals.direction != "centre"
                and held_run >= rules.STEP_FRAMES
                and signals.direction not in capture.steps_seen
            ):
                capture.steps_seen.append(signals.direction)

    # Depth is only visible once the head has moved: compare the resting mesh
    # with the frame that moved furthest from it.
    turned = [(abs(f.yaw), f.index) for f in capture.frames]
    if turned:
        capture.turn_degrees, furthest = max(turned)
        mesh = meshes[furthest]
        if mesh is not None and capture.turn_degrees >= rules.TURN_DEGREES:
            capture.planarity = planarity(resting, mesh)
            for frame in capture.frames:
                if frame.index == furthest:
                    frame.planarity = capture.planarity
    return capture
