"""
Kinematics Engine — Head Pose Estimator

Estimates head pitch, yaw, and roll from MediaPipe FaceMesh landmarks
using OpenCV's solvePnP. Also detects head movement jitter (erratic
movement) and freeze (no movement for extended periods).

Uses a canonical 3D face model and 2D-3D point correspondences to solve
for the rotation vector, which is then decomposed into Euler angles.

Key landmarks used for PnP:
    - Nose tip (1)
    - Chin (152)
    - Left eye outer corner (33)
    - Right eye outer corner (263)
    - Left mouth corner (61)
    - Right mouth corner (291)
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass

import cv2
import numpy as np
from numpy.typing import NDArray

logger = logging.getLogger(__name__)

# Canonical 3D face model points (approximate, in arbitrary units)
# These correspond to the 6 key landmarks used for PnP solving
_MODEL_POINTS_3D = np.array(
    [
        [0.0, 0.0, 0.0],  # Nose tip
        [0.0, -63.6, -12.5],  # Chin
        [-43.3, 32.7, -26.0],  # Left eye outer corner
        [43.3, 32.7, -26.0],  # Right eye outer corner
        [-28.9, -28.9, -24.1],  # Left mouth corner
        [28.9, -28.9, -24.1],  # Right mouth corner
    ],
    dtype=np.float64,
)

# MediaPipe FaceMesh landmark indices for the 6 PnP points
_PNP_LANDMARK_INDICES = [1, 152, 33, 263, 61, 291]

# Jitter detection: angular velocity threshold (degrees/frame)
_JITTER_THRESHOLD_DEG = 3.0

# Freeze detection: minimum angular movement over window
_FREEZE_THRESHOLD_DEG = 0.5
_FREEZE_WINDOW_FRAMES = 90  # 3 seconds at 30fps


@dataclass(frozen=True)
class HeadPoseResult:
    """Head pose estimation result for a single frame."""

    pitch: float  # Degrees, positive = looking up
    yaw: float  # Degrees, positive = looking right
    roll: float  # Degrees, positive = tilting right
    is_jittery: bool  # Rapid head movements detected
    is_frozen: bool  # No significant movement
    angular_velocity: float  # Degrees/frame, magnitude of rotation change


class HeadPoseEstimator:
    """
    Estimates head pose from FaceMesh landmarks using solvePnP.

    Maintains a history of pose estimates for jitter and freeze detection.

    Usage:
        estimator = HeadPoseEstimator(frame_width=640, frame_height=480)
        result = estimator.update(landmarks_px, timestamp)
    """

    def __init__(
        self,
        frame_width: int = 640,
        frame_height: int = 480,
        jitter_threshold_deg: float = _JITTER_THRESHOLD_DEG,
        freeze_threshold_deg: float = _FREEZE_THRESHOLD_DEG,
        freeze_window_frames: int = _FREEZE_WINDOW_FRAMES,
        history_size: int = 150,  # 5 seconds at 30fps
    ) -> None:
        self._frame_width = frame_width
        self._frame_height = frame_height
        self._jitter_threshold = jitter_threshold_deg
        self._freeze_threshold = freeze_threshold_deg
        self._freeze_window = freeze_window_frames

        # Camera matrix (approximate, using frame dimensions)
        focal_length = frame_width
        center = (frame_width / 2.0, frame_height / 2.0)
        self._camera_matrix = np.array(
            [
                [focal_length, 0, center[0]],
                [0, focal_length, center[1]],
                [0, 0, 1],
            ],
            dtype=np.float64,
        )
        self._dist_coeffs = np.zeros((4, 1), dtype=np.float64)

        # Pose history for jitter/freeze detection
        self._pose_history: deque[tuple[float, float, float]] = deque(
            maxlen=history_size
        )

        # Previous pose for angular velocity
        self._prev_pose: tuple[float, float, float] | None = None
        self._latest_result: HeadPoseResult | None = None

    @property
    def latest_result(self) -> HeadPoseResult | None:
        """Most recent head pose result."""
        return self._latest_result

    def update(
        self,
        landmarks_px: NDArray[np.floating],
        timestamp: float = 0.0,
    ) -> HeadPoseResult:
        """
        Estimate head pose from face landmarks.

        Args:
            landmarks_px: Full face landmarks in pixel coords, shape (478, 2).
            timestamp: Frame timestamp (unused currently, reserved).

        Returns:
            HeadPoseResult with pitch, yaw, roll and movement indicators.
        """
        # Extract the 6 PnP landmark points
        image_points = landmarks_px[_PNP_LANDMARK_INDICES].astype(np.float64)

        # Solve PnP
        pitch, yaw, roll = self._solve_head_pose(image_points)

        # Compute angular velocity
        angular_velocity = self._compute_angular_velocity(pitch, yaw, roll)

        # Update history
        self._pose_history.append((pitch, yaw, roll))
        self._prev_pose = (pitch, yaw, roll)

        # Detect jitter and freeze
        is_jittery = angular_velocity > self._jitter_threshold
        is_frozen = self._detect_freeze()

        result = HeadPoseResult(
            pitch=pitch,
            yaw=yaw,
            roll=roll,
            is_jittery=is_jittery,
            is_frozen=is_frozen,
            angular_velocity=angular_velocity,
        )

        self._latest_result = result
        return result

    def _solve_head_pose(
        self, image_points: NDArray[np.float64]
    ) -> tuple[float, float, float]:
        """
        Solve for head pose using solvePnP.

        Args:
            image_points: (6, 2) array of 2D landmark coordinates.

        Returns:
            (pitch, yaw, roll) in degrees.
        """
        success, rotation_vec, translation_vec = cv2.solvePnP(
            _MODEL_POINTS_3D,
            image_points,
            self._camera_matrix,
            self._dist_coeffs,
            flags=cv2.SOLVEPNP_ITERATIVE,
        )

        if not success:
            logger.debug("solvePnP failed")
            return 0.0, 0.0, 0.0

        # Convert rotation vector to rotation matrix
        rotation_mat, _ = cv2.Rodrigues(rotation_vec)

        # Decompose rotation matrix to Euler angles
        pitch, yaw, roll = self._rotation_matrix_to_euler(rotation_mat)

        return pitch, yaw, roll

    @staticmethod
    def _rotation_matrix_to_euler(
        rotation_mat: NDArray[np.float64],
    ) -> tuple[float, float, float]:
        """
        Convert a 3x3 rotation matrix to Euler angles (pitch, yaw, roll).

        Uses the decomposition from Slabaugh (1999) to handle gimbal lock.

        Returns:
            (pitch, yaw, roll) in degrees.
        """
        # Extract angles using atan2 for robustness
        sy = np.sqrt(rotation_mat[0, 0] ** 2 + rotation_mat[1, 0] ** 2)

        if sy > 1e-6:
            pitch = np.arctan2(rotation_mat[2, 1], rotation_mat[2, 2])
            yaw = np.arctan2(-rotation_mat[2, 0], sy)
            roll = np.arctan2(rotation_mat[1, 0], rotation_mat[0, 0])
        else:
            # Gimbal lock
            pitch = np.arctan2(-rotation_mat[1, 2], rotation_mat[1, 1])
            yaw = np.arctan2(-rotation_mat[2, 0], sy)
            roll = 0.0

        # Convert to degrees
        pitch_deg = float(np.degrees(pitch))
        yaw_deg = float(np.degrees(yaw))
        roll_deg = float(np.degrees(roll))

        return pitch_deg, yaw_deg, roll_deg

    def _compute_angular_velocity(
        self, pitch: float, yaw: float, roll: float
    ) -> float:
        """
        Compute angular velocity (degrees/frame) from the previous pose.

        Returns:
            Angular velocity magnitude in degrees/frame.
        """
        if self._prev_pose is None:
            return 0.0

        dp = pitch - self._prev_pose[0]
        dy = yaw - self._prev_pose[1]
        dr = roll - self._prev_pose[2]

        return float(np.sqrt(dp**2 + dy**2 + dr**2))

    def _detect_freeze(self) -> bool:
        """
        Detect if the head has been stationary for an extended period.

        Checks if the total angular displacement over the freeze window
        is below the freeze threshold.
        """
        if len(self._pose_history) < self._freeze_window:
            return False

        # Get the most recent freeze_window poses
        recent = list(self._pose_history)[-self._freeze_window:]

        # Compute total angular range
        pitches = [p[0] for p in recent]
        yaws = [p[1] for p in recent]
        rolls = [p[2] for p in recent]

        pitch_range = max(pitches) - min(pitches)
        yaw_range = max(yaws) - min(yaws)
        roll_range = max(rolls) - min(rolls)

        total_range = pitch_range + yaw_range + roll_range
        return total_range < self._freeze_threshold

    def get_head_pose_features(self) -> dict[str, float | None]:
        """
        Get head pose features for KinematicFeatures.

        Returns:
            Dict with head_pitch, head_yaw, head_roll.
        """
        result = self._latest_result
        if result is None:
            return {
                "head_pitch": None,
                "head_yaw": None,
                "head_roll": None,
            }

        return {
            "head_pitch": result.pitch,
            "head_yaw": result.yaw,
            "head_roll": result.roll,
        }

    def reset(self) -> None:
        """Reset all state."""
        self._pose_history.clear()
        self._prev_pose = None
        self._latest_result = None
