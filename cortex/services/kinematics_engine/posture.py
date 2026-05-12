"""
Kinematics Engine — Posture Analyzer

Tracks shoulder posture and forward lean from MediaPipe Pose landmarks.
Computes slump score (composite 0-1) and detects posture collapse.

Metrics:
- Shoulder drop ratio: vertical displacement of shoulder midpoint
  vs calibrated neutral, normalized by torso length
- Forward lean angle: angle between shoulder-ear line and vertical
- Slump score: composite of shoulder drop + forward lean (0-1)
- Posture collapse: shoulder drop > 15% + forward lean > 20°

Since MediaPipe Pose requires a full body detector (separate from FaceMesh),
this module can also work with FaceMesh landmarks by using ear and chin
landmarks as shoulder proxies when pose landmarks are unavailable.

Primary mode: Uses shoulder landmarks (11, 12) from MediaPipe Pose
Fallback mode: Uses FaceMesh landmarks for approximate lean estimation
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from cortex.libs.config.settings import PostureSignalConfig

logger = logging.getLogger(__name__)

# FaceMesh landmark indices for fallback posture estimation
_FACEMESH_LEFT_EAR = 234
_FACEMESH_RIGHT_EAR = 454
_FACEMESH_NOSE_TIP = 1
_FACEMESH_CHIN = 152
_FACEMESH_FOREHEAD = 10


@dataclass(frozen=True)
class PostureState:
    """Posture assessment for a single frame."""

    shoulder_drop_ratio: float | None  # 0-1, displacement from baseline
    forward_lean_angle: float | None  # Degrees from vertical
    forward_lean_score: float  # 0-1 normalized lean score
    slump_score: float  # 0-1 composite
    is_collapsed: bool  # Posture collapse detected
    has_pose_landmarks: bool  # Whether full pose landmarks were available


class PostureAnalyzer:
    """
    Analyzes posture from body/face landmarks.

    Tracks shoulder position and forward lean, comparing against a
    calibrated baseline to detect posture degradation.

    Supports two modes:
    1. Full pose mode: Using MediaPipe Pose shoulder landmarks (11, 12)
    2. FaceMesh-only mode: Estimating lean from ear-nose-chin geometry

    Usage:
        analyzer = PostureAnalyzer()
        # With full pose landmarks:
        state = analyzer.update_with_pose(pose_landmarks, timestamp)
        # Or with face-only landmarks:
        state = analyzer.update_with_face(face_landmarks_px, timestamp)
    """

    def __init__(
        self,
        config: PostureSignalConfig | None = None,
        history_size: int = 150,  # 5 seconds at 30fps
    ) -> None:
        self._config = config or PostureSignalConfig()

        # Baseline (calibrated neutral posture)
        self._baseline_shoulder_y: float | None = None
        self._baseline_torso_length: float | None = None
        self._baseline_lean_angle: float | None = None

        # History for smoothing
        self._drop_history: deque[float] = deque(maxlen=history_size)
        self._lean_history: deque[float] = deque(maxlen=history_size)

        # Calibration state
        self._calibration_samples: list[tuple[float, float]] = []  # (shoulder_y, torso_len)
        self._calibration_lean_samples: list[float] = []
        self._is_calibrated = False

        # Latest result
        self._latest_state: PostureState | None = None

    @property
    def latest_state(self) -> PostureState | None:
        """Most recent posture state."""
        return self._latest_state

    @property
    def is_calibrated(self) -> bool:
        """Whether baseline calibration is complete."""
        return self._is_calibrated

    def calibrate_from_samples(
        self,
        shoulder_y_values: list[float],
        torso_lengths: list[float],
        lean_angles: list[float] | None = None,
    ) -> None:
        """
        Set baseline from calibration samples.

        Args:
            shoulder_y_values: Y-coordinates of shoulder midpoint during neutral.
            torso_lengths: Torso length estimates during neutral.
            lean_angles: Forward lean angles during neutral (optional).
        """
        if shoulder_y_values and torso_lengths:
            self._baseline_shoulder_y = float(np.median(shoulder_y_values))
            self._baseline_torso_length = float(np.median(torso_lengths))
            self._is_calibrated = True
            logger.info(
                f"Posture calibrated: shoulder_y={self._baseline_shoulder_y:.1f}, "
                f"torso_length={self._baseline_torso_length:.1f}"
            )

        if lean_angles:
            self._baseline_lean_angle = float(np.median(lean_angles))

    def update_with_pose(
        self,
        pose_landmarks: NDArray[np.floating],
        timestamp: float = 0.0,
    ) -> PostureState:
        """
        Update posture from MediaPipe Pose landmarks (33 landmarks).

        Uses landmarks 11 (left shoulder) and 12 (right shoulder).

        Args:
            pose_landmarks: Pose landmarks in pixel coords, shape (33, 2) or (33, 3).
            timestamp: Frame timestamp.

        Returns:
            PostureState with shoulder drop, lean, and slump score.
        """
        # Extract shoulder positions
        left_shoulder = pose_landmarks[11, :2]
        right_shoulder = pose_landmarks[12, :2]

        shoulder_mid_y = float((left_shoulder[1] + right_shoulder[1]) / 2.0)
        shoulder_width = float(np.linalg.norm(left_shoulder[:2] - right_shoulder[:2]))

        # Estimate torso length as shoulder width * 1.5 (rough approximation)
        torso_length = shoulder_width * 1.5

        # Auto-calibrate on first samples if not calibrated
        if not self._is_calibrated:
            self._auto_calibrate(shoulder_mid_y, torso_length)

        # Compute shoulder drop ratio
        shoulder_drop = self._compute_shoulder_drop(shoulder_mid_y, torso_length)

        # Forward lean — approximate from shoulder position shift
        # In 2D, forward lean manifests as shoulder midpoint moving up in frame
        # (closer to camera = shoulders appear higher)
        # This is a rough proxy; full 3D lean requires depth
        lean_angle = 0.0  # Placeholder — difficult without depth
        lean_score = 0.0

        slump_score = shoulder_drop if shoulder_drop is not None else 0.0
        is_collapsed = (
            shoulder_drop is not None
            and shoulder_drop > self._config.shoulder_drop_threshold
        )

        if shoulder_drop is not None:
            self._drop_history.append(shoulder_drop)

        state = PostureState(
            shoulder_drop_ratio=shoulder_drop,
            forward_lean_angle=lean_angle,
            forward_lean_score=lean_score,
            slump_score=slump_score,
            is_collapsed=is_collapsed,
            has_pose_landmarks=True,
        )

        self._latest_state = state
        return state

    def update_with_face(
        self,
        face_landmarks_px: NDArray[np.floating],
        timestamp: float = 0.0,
    ) -> PostureState:
        """
        Estimate posture from FaceMesh landmarks (fallback mode).

        Uses ear-nose-chin geometry to estimate forward lean.
        Cannot compute shoulder drop without pose landmarks.

        The forward lean angle is estimated from the angle between
        the ear-midpoint → nose vector and the vertical axis.
        When leaning forward, the nose moves down relative to the ears.

        Args:
            face_landmarks_px: FaceMesh landmarks in pixel coords, shape (478, 2).
            timestamp: Frame timestamp.

        Returns:
            PostureState with lean estimates (no shoulder drop).
        """
        # Extract key landmarks
        left_ear = face_landmarks_px[_FACEMESH_LEFT_EAR]
        right_ear = face_landmarks_px[_FACEMESH_RIGHT_EAR]
        face_landmarks_px[_FACEMESH_NOSE_TIP]
        chin = face_landmarks_px[_FACEMESH_CHIN]
        forehead = face_landmarks_px[_FACEMESH_FOREHEAD]

        # Ear midpoint
        (left_ear + right_ear) / 2.0

        # Forward lean: angle between vertical and ear-midpoint → chin vector
        # In upright posture, chin is roughly below ear midpoint
        # Forward lean shifts chin forward (rightward in some views)
        #
        # Use the forehead-chin line angle relative to vertical
        face_vector = chin - forehead  # Points from forehead to chin
        vertical = np.array([0.0, 1.0])  # Pointing down in image coords

        # Compute angle between face vector and vertical
        face_len = np.linalg.norm(face_vector)
        if face_len < 1e-6:
            lean_angle = 0.0
        else:
            cos_angle = np.dot(face_vector, vertical) / face_len
            cos_angle = np.clip(cos_angle, -1.0, 1.0)
            lean_angle = float(np.degrees(np.arccos(cos_angle)))

        # Adjust for baseline if available
        if self._baseline_lean_angle is not None:
            lean_angle = lean_angle - self._baseline_lean_angle

        lean_angle = max(0.0, lean_angle)

        # Normalize lean to score (0-1)
        # 0° = upright, 45° = maximum measurable lean
        lean_score = float(np.clip(lean_angle / 45.0, 0.0, 1.0))

        # Slump score based on lean only (no shoulder data)
        slump_score = lean_score

        # Posture collapse from lean alone
        is_collapsed = lean_angle > self._config.forward_lean_threshold

        self._lean_history.append(lean_angle)

        state = PostureState(
            shoulder_drop_ratio=None,
            forward_lean_angle=lean_angle,
            forward_lean_score=lean_score,
            slump_score=slump_score,
            is_collapsed=is_collapsed,
            has_pose_landmarks=False,
        )

        self._latest_state = state
        return state

    def _compute_shoulder_drop(
        self, shoulder_mid_y: float, torso_length: float
    ) -> float | None:
        """
        Compute shoulder drop ratio relative to baseline.

        Positive values mean shoulders have dropped (slouching).
        Normalized by torso length so it's scale-invariant.

        Returns:
            Drop ratio (0-1+), or None if not calibrated.
        """
        if self._baseline_shoulder_y is None or self._baseline_torso_length is None:
            return None

        # In image coordinates, Y increases downward
        # Shoulder drop = shoulders moving down = Y increasing
        displacement = shoulder_mid_y - self._baseline_shoulder_y

        # Normalize by torso length
        if self._baseline_torso_length < 1e-6:
            return None

        drop_ratio = displacement / self._baseline_torso_length
        return float(np.clip(drop_ratio, 0.0, 1.0))

    def _auto_calibrate(self, shoulder_mid_y: float, torso_length: float) -> None:
        """
        Auto-calibrate from the first N frames.

        Collects samples and sets baseline once enough are gathered.
        """
        self._calibration_samples.append((shoulder_mid_y, torso_length))

        # Calibrate after 30 frames (~1 second)
        if len(self._calibration_samples) >= 30:
            ys = [s[0] for s in self._calibration_samples]
            tls = [s[1] for s in self._calibration_samples]
            self._baseline_shoulder_y = float(np.median(ys))
            self._baseline_torso_length = float(np.median(tls))
            self._is_calibrated = True
            self._calibration_samples.clear()
            logger.info(
                f"Auto-calibrated posture: shoulder_y={self._baseline_shoulder_y:.1f}, "
                f"torso_length={self._baseline_torso_length:.1f}"
            )

    def get_posture_features(self) -> dict[str, float | None]:
        """
        Get posture features for KinematicFeatures.

        Returns:
            Dict with slump_score, forward_lean_score, shoulder_drop_ratio.
        """
        state = self._latest_state
        if state is None:
            return {
                "slump_score": None,
                "forward_lean_score": None,
                "shoulder_drop_ratio": None,
            }

        return {
            "slump_score": state.slump_score,
            "forward_lean_score": state.forward_lean_score,
            "shoulder_drop_ratio": state.shoulder_drop_ratio,
        }

    def get_smoothed_slump(self, window: int = 30) -> float:
        """
        Get smoothed slump score over a rolling window.

        Args:
            window: Number of frames to average over.

        Returns:
            Smoothed slump score (0-1).
        """
        if not self._drop_history and not self._lean_history:
            return 0.0

        scores: list[float] = []
        if self._drop_history:
            drops = list(self._drop_history)[-window:]
            scores.extend(drops)
        if self._lean_history:
            leans = list(self._lean_history)[-window:]
            lean_scores = [l / 45.0 for l in leans]
            scores.extend(lean_scores)

        if not scores:
            return 0.0

        return float(np.clip(np.mean(scores), 0.0, 1.0))

    def reset(self) -> None:
        """Reset all state (preserves calibration)."""
        self._drop_history.clear()
        self._lean_history.clear()
        self._latest_state = None

    def reset_calibration(self) -> None:
        """Reset calibration and all state."""
        self.reset()
        self._baseline_shoulder_y = None
        self._baseline_torso_length = None
        self._baseline_lean_angle = None
        self._calibration_samples.clear()
        self._calibration_lean_samples.clear()
        self._is_calibrated = False
