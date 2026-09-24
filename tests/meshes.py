"""Synthetic face meshes, so the liveness tests can set pose, blink and flatness.

A real mesh is 478 points with depth. These build one from a fixed random cloud
and rotate it, so the pose maths recovers the angle it was given. A "flat" cloud
has no depth, which is what a printed photo looks like to the planarity check.
"""

import numpy as np

from app.engines.mesh import _LEFT_EYE, _RIGHT_EYE, Mesh

_RNG = np.random.default_rng(11)
# A face-shaped cloud: 100mm wide, 130 tall, 60 deep.
_CLOUD = _RNG.normal(size=(478, 3)) * np.array([50.0, 65.0, 30.0])


def _set_eyes(points: np.ndarray, opening: float) -> None:
    """Put the eye points where they give this eye aspect ratio."""
    for eye, centre in ((_LEFT_EYE, -30.0), (_RIGHT_EYE, 30.0)):
        (top, bottom, corners) = eye
        width = 30.0
        points[corners[0]] = [centre - width / 2, 0.0, 0.0]
        points[corners[1]] = [centre + width / 2, 0.0, 0.0]
        for t, b in zip(top, bottom):
            points[t] = [centre, -opening * width / 2, 0.0]
            points[b] = [centre, opening * width / 2, 0.0]


def mesh(yaw: float = 0.0, pitch: float = 0.0, opening: float = 0.30, flat: bool = False) -> Mesh:
    points = _CLOUD.copy()
    if flat:
        points[:, 2] = 0.0          # a photograph: no depth at all
    _set_eyes(points, opening)

    y, p = np.radians(yaw), np.radians(pitch)
    around_y = np.array([[np.cos(y), 0, np.sin(y)], [0, 1, 0], [-np.sin(y), 0, np.cos(y)]])
    around_x = np.array([[1, 0, 0], [0, np.cos(p), -np.sin(p)], [0, np.sin(p), np.cos(p)]])
    return Mesh(points=(points @ around_y.T @ around_x.T).astype(np.float32), presence=1.0)
