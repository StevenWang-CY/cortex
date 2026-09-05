"""Circular (angle) arithmetic shared by pose estimation and calibration.

Head-pose angles are periodic; linear differences and means silently break
near the +/-180 degree wrap.  These helpers are numpy-only so the calibration
runner and posture proxy can use them without importing native vision
dependencies.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from numpy.typing import NDArray


def wrapped_angle_delta(current_deg: float, previous_deg: float) -> float:
    """Return ``current - previous`` wrapped into ``[-180, 180)`` degrees."""

    return (float(current_deg) - float(previous_deg) + 180.0) % 360.0 - 180.0


def circular_mean_deg(values_deg: NDArray[np.floating[Any]] | list[float]) -> float:
    """Circular mean of angles in degrees, in ``[-180, 180]``.

    Averaging raw solvePnP pitches is wrong near the wrap: the linear mean of
    ``{179.5, -179.5}`` is ``0`` while the angles are one degree apart.
    """

    angles = np.radians(np.asarray(values_deg, dtype=np.float64).reshape(-1))
    if angles.size == 0:
        raise ValueError("circular mean requires at least one angle")
    if not bool(np.isfinite(angles).all()):
        raise ValueError("circular mean requires finite angles")
    return float(np.degrees(np.arctan2(np.mean(np.sin(angles)), np.mean(np.cos(angles)))))
