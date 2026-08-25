"""Camera-relative head/neck flexion proxy.

Cortex does not run a body-pose model in production, so this module makes no
shoulder or whole-posture claim. It compares solvePnP head pitch with an
explicit, camera-bound neutral calibration and tracks above-threshold dwell in
monotonic time.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

from cortex.libs.config.settings import PostureSignalConfig


@dataclass(frozen=True)
class PostureState:
    """Compatibility name for one head/neck proxy observation."""

    head_neck_flexion_angle: float | None
    head_neck_flexion_score: float | None
    sustained_flexion_seconds: float
    is_sustained: bool
    calibrated: bool
    proxy_available: bool
    invalidated_reason: str | None = None

    @property
    def forward_lean_angle(self) -> float | None:
        return self.head_neck_flexion_angle

    @property
    def forward_lean_score(self) -> float | None:
        return self.head_neck_flexion_score

    @property
    def slump_score(self) -> float | None:
        return self.head_neck_flexion_score

    @property
    def shoulder_drop_ratio(self) -> None:
        return None

    @property
    def is_collapsed(self) -> bool:
        return self.is_sustained

    @property
    def has_pose_landmarks(self) -> bool:
        return False


class PostureAnalyzer:
    """Camera-bound, calibrated head/neck flexion proxy."""

    def __init__(self, config: PostureSignalConfig | None = None) -> None:
        self._config = config or PostureSignalConfig()
        self._neutral_pitch_deg: float | None = None
        self._neutral_face_scale: float | None = None
        self._camera_identity_key: str | None = None
        self._above_threshold_since: float | None = None
        self._last_timestamp: float | None = None
        self._latest_state: PostureState | None = None
        self._invalidated_reason: str | None = "calibration_required"

    @property
    def latest_state(self) -> PostureState | None:
        return self._latest_state

    @property
    def is_calibrated(self) -> bool:
        return (
            self._neutral_pitch_deg is not None
            and self._neutral_face_scale is not None
            and self._camera_identity_key is not None
        )

    @staticmethod
    def face_scale(face_landmarks_px: NDArray[np.floating[Any]]) -> float:
        landmarks = np.asarray(face_landmarks_px, dtype=np.float64)
        if landmarks.ndim != 2 or landmarks.shape[1] < 2:
            raise ValueError("face landmarks must be an (N, 2+) array")
        xy = landmarks[:, :2]
        if not bool(np.isfinite(xy).all()):
            raise ValueError("face landmarks must be finite")
        low = np.percentile(xy, 5.0, axis=0)
        high = np.percentile(xy, 95.0, axis=0)
        width, height = high - low
        scale = float(np.sqrt(max(0.0, width) * max(0.0, height)))
        if scale <= 1e-6:
            raise ValueError("face scale is degenerate")
        return scale

    def apply_calibration(
        self,
        *,
        neutral_pitch_deg: float,
        neutral_face_scale: float,
        camera_identity_key: str,
    ) -> None:
        if not math.isfinite(neutral_pitch_deg):
            raise ValueError("neutral pitch must be finite")
        if not math.isfinite(neutral_face_scale) or neutral_face_scale <= 0:
            raise ValueError("neutral face scale must be finite and positive")
        if not camera_identity_key:
            raise ValueError("camera identity key is required")
        self._neutral_pitch_deg = float(neutral_pitch_deg)
        self._neutral_face_scale = float(neutral_face_scale)
        self._camera_identity_key = camera_identity_key
        self._invalidated_reason = None
        self.reset()

    def update(
        self,
        *,
        pitch_deg: float,
        face_scale: float,
        timestamp: float,
        camera_identity_key: str,
    ) -> PostureState:
        now = self._validate_time(timestamp)
        if not math.isfinite(pitch_deg) or not math.isfinite(face_scale) or face_scale <= 0:
            raise ValueError("head/neck inputs must be finite and face scale positive")

        if self._camera_identity_key is not None and (
            camera_identity_key != self._camera_identity_key
        ):
            self._invalidate("camera_identity_changed")
        if self._neutral_face_scale is not None:
            scale_change = abs(face_scale / self._neutral_face_scale - 1.0)
            if scale_change > self._config.face_scale_change_tolerance:
                self._invalidate("face_scale_changed")

        if not self.is_calibrated:
            self._above_threshold_since = None
            state = PostureState(
                head_neck_flexion_angle=None,
                head_neck_flexion_score=None,
                sustained_flexion_seconds=0.0,
                is_sustained=False,
                calibrated=False,
                proxy_available=False,
                invalidated_reason=self._invalidated_reason or "calibration_required",
            )
            self._latest_state = state
            self._last_timestamp = now
            return state

        assert self._neutral_pitch_deg is not None
        flexion = max(0.0, float(pitch_deg) - self._neutral_pitch_deg)
        score = float(np.clip(flexion / 45.0, 0.0, 1.0))
        prior_gap = (
            None if self._last_timestamp is None else now - self._last_timestamp
        )
        if prior_gap is not None and prior_gap * 1000.0 > self._config.max_valid_gap_ms:
            self._above_threshold_since = None
        if flexion >= self._config.head_neck_flexion_threshold_deg:
            if self._above_threshold_since is None:
                self._above_threshold_since = now
            dwell = now - self._above_threshold_since
        else:
            self._above_threshold_since = None
            dwell = 0.0
        sustained = dwell >= self._config.head_neck_sustain_seconds
        state = PostureState(
            head_neck_flexion_angle=flexion,
            head_neck_flexion_score=score,
            sustained_flexion_seconds=dwell,
            is_sustained=sustained,
            calibrated=True,
            proxy_available=True,
        )
        self._latest_state = state
        self._last_timestamp = now
        return state

    def observe_missing(self, timestamp: float) -> None:
        now = self._validate_time(timestamp)
        self._above_threshold_since = None
        self._last_timestamp = now
        self._latest_state = None

    def _validate_time(self, timestamp: float) -> float:
        now = float(timestamp)
        if not math.isfinite(now) or now < 0:
            raise ValueError("head/neck timestamp must be finite and non-negative")
        if self._last_timestamp is not None and now <= self._last_timestamp:
            raise ValueError("head/neck timestamps must be strictly increasing")
        return now

    def _invalidate(self, reason: str) -> None:
        self._neutral_pitch_deg = None
        self._neutral_face_scale = None
        self._camera_identity_key = None
        self._above_threshold_since = None
        self._invalidated_reason = reason

    def get_posture_features(self) -> dict[str, float | bool | str | None]:
        state = self._latest_state
        if state is None:
            return {
                "head_neck_flexion_angle": None,
                "head_neck_flexion_score": None,
                "head_neck_flexion_dwell_seconds": None,
                "head_neck_proxy_available": False,
                "head_neck_unavailable_reason": self._invalidated_reason,
            }
        return {
            "head_neck_flexion_angle": state.head_neck_flexion_angle,
            "head_neck_flexion_score": state.head_neck_flexion_score,
            "head_neck_flexion_dwell_seconds": state.sustained_flexion_seconds,
            "head_neck_proxy_available": state.proxy_available,
            "head_neck_unavailable_reason": state.invalidated_reason,
        }

    def get_smoothed_slump(self, window: int = 30) -> float:
        del window
        state = self._latest_state
        return float(state.head_neck_flexion_score or 0.0) if state else 0.0

    def reset(self) -> None:
        self._above_threshold_since = None
        self._last_timestamp = None
        self._latest_state = None

    def reset_calibration(self) -> None:
        self.reset()
        self._neutral_pitch_deg = None
        self._neutral_face_scale = None
        self._camera_identity_key = None
        self._invalidated_reason = "calibration_required"
