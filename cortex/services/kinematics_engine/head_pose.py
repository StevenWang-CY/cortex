"""Monotonic-time head-pose proxy from FaceMesh landmarks."""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np
from numpy.typing import NDArray

from cortex.libs.signal.angles import circular_mean_deg, wrapped_angle_delta

__all__ = [
    "HeadPoseEstimator",
    "HeadPoseResult",
    "circular_mean_deg",
    "wrapped_angle_delta",
]

logger = logging.getLogger(__name__)

# Generic 3D face model in the *anthropometric* convention used by most
# published tables: x right, y up, z toward the camera (millimetres).
# Order: nose tip, chin, left eye outer corner, right eye outer corner,
# left mouth corner, right mouth corner.
_MODEL_POINTS_3D = np.array(
    [
        [0.0, 0.0, 0.0],
        [0.0, -63.6, -12.5],
        [-43.3, 32.7, -26.0],
        [43.3, 32.7, -26.0],
        [-28.9, -28.9, -24.1],
        [28.9, -28.9, -24.1],
    ],
    dtype=np.float64,
)
# OpenCV camera coordinates are x right, y *down*, z *away* from the camera,
# i.e. the model above rotated by 180 degrees about x.  Solving PnP against
# the un-rotated table made a frontal face resolve to R = Rx(180 deg), so
# pitch sat at +/-180 deg and wrapped through the neutral pose.  Solving
# against the camera-convention model makes a frontal face (0, 0, 0) and
# head flexion (looking down: forehead toward the camera, chin tucked)
# a positive pitch.
_MODEL_POINTS_3D_CAMERA = _MODEL_POINTS_3D * np.array([1.0, -1.0, -1.0])
_PNP_LANDMARK_INDICES = [1, 152, 33, 263, 61, 291]


@dataclass(frozen=True)
class HeadPoseResult:
    pitch: float
    yaw: float
    roll: float
    is_jittery: bool
    is_frozen: bool
    angular_velocity_deg_per_s: float
    valid_history_seconds: float

    @property
    def angular_velocity(self) -> float:
        """Compatibility alias; the unit is now explicitly degrees/second."""

        return self.angular_velocity_deg_per_s


class HeadPoseEstimator:
    """Estimate pose and time-derived motion without frame-count assumptions."""

    def __init__(
        self,
        frame_width: int = 640,
        frame_height: int = 480,
        jitter_threshold_deg_per_s: float = 90.0,
        freeze_threshold_deg: float = 0.5,
        freeze_window_seconds: float = 3.0,
        history_seconds: float = 5.0,
        max_valid_gap_ms: float = 250.0,
        *,
        jitter_threshold_deg: float | None = None,
        freeze_window_frames: int | None = None,
    ) -> None:
        # Decode-only compatibility for older callers. Values keep their old
        # 30 Hz meaning but are normalized immediately into physical units.
        if jitter_threshold_deg is not None:
            jitter_threshold_deg_per_s = float(jitter_threshold_deg) * 30.0
        if freeze_window_frames is not None:
            freeze_window_seconds = float(freeze_window_frames) / 30.0
        if frame_width <= 0 or frame_height <= 0:
            raise ValueError("head-pose frame dimensions must be positive")
        if jitter_threshold_deg_per_s <= 0 or freeze_window_seconds <= 0:
            raise ValueError("head-pose temporal thresholds must be positive")
        self._jitter_threshold = float(jitter_threshold_deg_per_s)
        self._freeze_threshold = float(freeze_threshold_deg)
        self._freeze_window_seconds = float(freeze_window_seconds)
        self._history_seconds = max(float(history_seconds), self._freeze_window_seconds)
        self._max_valid_gap_seconds = float(max_valid_gap_ms) / 1000.0

        focal_length = float(frame_width)
        center = (frame_width / 2.0, frame_height / 2.0)
        self._camera_matrix = np.array(
            [
                [focal_length, 0.0, center[0]],
                [0.0, focal_length, center[1]],
                [0.0, 0.0, 1.0],
            ],
            dtype=np.float64,
        )
        self._dist_coeffs = np.zeros((4, 1), dtype=np.float64)
        self._pose_history: deque[tuple[float, float, float, float]] = deque()
        self._previous: tuple[float, float, float, float] | None = None
        self._last_observed_at: float | None = None
        self._latest_result: HeadPoseResult | None = None

    @property
    def latest_result(self) -> HeadPoseResult | None:
        return self._latest_result

    def update(
        self,
        landmarks_px: NDArray[np.floating[Any]],
        timestamp: float,
    ) -> HeadPoseResult:
        now = self._validate_time(timestamp)
        landmarks = np.asarray(landmarks_px, dtype=np.float64)
        image_points = landmarks[_PNP_LANDMARK_INDICES]
        if image_points.shape != (6, 2) or not bool(np.isfinite(image_points).all()):
            raise ValueError("head-pose landmarks must be finite")
        pitch, yaw, roll = self._solve_head_pose(image_points)

        velocity = 0.0
        if self._previous is not None:
            previous_time, old_pitch, old_yaw, old_roll = self._previous
            elapsed = now - previous_time
            if elapsed <= self._max_valid_gap_seconds:
                deltas = np.asarray(
                    [
                        self._wrapped_delta(pitch, old_pitch),
                        self._wrapped_delta(yaw, old_yaw),
                        self._wrapped_delta(roll, old_roll),
                    ],
                    dtype=np.float64,
                )
                velocity = float(np.linalg.norm(deltas) / elapsed)

        self._pose_history.append((now, pitch, yaw, roll))
        self._previous = (now, pitch, yaw, roll)
        self._last_observed_at = now
        self._prune(now)
        valid_history_seconds = self._contiguous_history_seconds()
        result = HeadPoseResult(
            pitch=pitch,
            yaw=yaw,
            roll=roll,
            is_jittery=velocity > self._jitter_threshold,
            is_frozen=self._detect_freeze(now),
            angular_velocity_deg_per_s=velocity,
            valid_history_seconds=valid_history_seconds,
        )
        self._latest_result = result
        return result

    def observe_missing(self, timestamp: float) -> None:
        now = self._validate_time(timestamp)
        self._previous = None
        self._last_observed_at = now
        # Freeze requires one contiguous visible interval. Keeping older
        # points would let a camera outage satisfy the dwell gate.
        self._pose_history.clear()
        self._latest_result = None

    def _validate_time(self, timestamp: float) -> float:
        now = float(timestamp)
        if not math.isfinite(now) or now < 0:
            raise ValueError("head-pose timestamp must be finite and non-negative")
        if self._last_observed_at is not None and now <= self._last_observed_at:
            raise ValueError("head-pose timestamps must be strictly increasing")
        return now

    def _solve_head_pose(
        self,
        image_points: NDArray[np.float64],
    ) -> tuple[float, float, float]:
        success, rotation_vec, _translation_vec = cv2.solvePnP(
            _MODEL_POINTS_3D_CAMERA,
            image_points,
            self._camera_matrix,
            self._dist_coeffs,
            flags=cv2.SOLVEPNP_ITERATIVE,
        )
        if not success:
            raise ValueError("solvePnP could not estimate head pose")
        rotation_mat, _ = cv2.Rodrigues(rotation_vec)
        return self._rotation_matrix_to_euler(
            np.asarray(rotation_mat, dtype=np.float64)
        )

    @staticmethod
    def _rotation_matrix_to_euler(
        rotation_mat: NDArray[np.float64],
    ) -> tuple[float, float, float]:
        sy = float(np.sqrt(rotation_mat[0, 0] ** 2 + rotation_mat[1, 0] ** 2))
        if sy > 1e-6:
            pitch = np.arctan2(rotation_mat[2, 1], rotation_mat[2, 2])
            yaw = np.arctan2(-rotation_mat[2, 0], sy)
            roll = np.arctan2(rotation_mat[1, 0], rotation_mat[0, 0])
        else:
            pitch = np.arctan2(-rotation_mat[1, 2], rotation_mat[1, 1])
            yaw = np.arctan2(-rotation_mat[2, 0], sy)
            roll = 0.0
        return (
            float(np.degrees(pitch)),
            float(np.degrees(yaw)),
            float(np.degrees(roll)),
        )

    @staticmethod
    def _wrapped_delta(current: float, previous: float) -> float:
        return wrapped_angle_delta(current, previous)

    def _prune(self, now: float) -> None:
        cutoff = now - self._history_seconds
        while self._pose_history and self._pose_history[0][0] < cutoff:
            self._pose_history.popleft()

    def _contiguous_history_seconds(self) -> float:
        if len(self._pose_history) < 2:
            return 0.0
        points = list(self._pose_history)
        start_index = len(points) - 1
        for index in range(len(points) - 1, 0, -1):
            if points[index][0] - points[index - 1][0] > self._max_valid_gap_seconds:
                break
            start_index = index - 1
        return points[-1][0] - points[start_index][0]

    def _detect_freeze(self, now: float) -> bool:
        # A tolerance for sparse sampling must never stand in for dwell.
        # Require an actually observed, contiguous interval spanning the
        # complete freeze window before looking at angular range.
        if self._contiguous_history_seconds() + 1e-9 < self._freeze_window_seconds:
            return False
        cutoff = now - self._freeze_window_seconds
        recent = [point for point in self._pose_history if point[0] >= cutoff]
        if len(recent) < 2:
            return False
        if any(
            right[0] - left[0] > self._max_valid_gap_seconds
            for left, right in zip(recent, recent[1:], strict=False)
        ):
            return False
        ranges = [
            max(point[axis] for point in recent) - min(point[axis] for point in recent)
            for axis in (1, 2, 3)
        ]
        return sum(ranges) < self._freeze_threshold

    def get_head_pose_features(self) -> dict[str, float | bool | None]:
        result = self._latest_result
        if result is None:
            return {
                "head_pitch": None,
                "head_yaw": None,
                "head_roll": None,
                "head_angular_velocity_deg_per_s": None,
                "head_is_jittery": None,
                "head_is_frozen": None,
            }
        return {
            "head_pitch": result.pitch,
            "head_yaw": result.yaw,
            "head_roll": result.roll,
            "head_angular_velocity_deg_per_s": result.angular_velocity_deg_per_s,
            "head_is_jittery": result.is_jittery,
            "head_is_frozen": result.is_frozen,
        }

    def reset(self) -> None:
        self._pose_history.clear()
        self._previous = None
        self._last_observed_at = None
        self._latest_result = None
