"""
Kinematics Engine — Blink Detector

Eye Aspect Ratio (EAR) based blink detection from MediaPipe FaceMesh
landmarks. Tracks blink events, blink rate (rolling 60s window), and
blink suppression score for cognitive load detection.

EAR algorithm (Soukupová & Čech, 2016):
    EAR = (||p2 - p6|| + ||p3 - p5||) / (2 * ||p1 - p4||)

where p1-p6 are the eye landmarks in standard order:
    p1 = outer corner, p2 = upper-outer, p3 = upper-inner,
    p4 = inner corner, p5 = lower-inner, p6 = lower-outer

Blink detection:
    - EAR drops below threshold (0.21) for >= 3 consecutive frames
    - EAR recovers above recovery threshold (0.25)

Normal resting blink rate: 15-20/min
Blink suppression (high cognitive load): < 8/min
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

from cortex.libs.config.settings import BlinkSignalConfig, LandmarksConfig

logger = logging.getLogger(__name__)

# Default baseline blink rate (normal resting)
_DEFAULT_BASELINE_BLINK_RATE = 17.0  # blinks/min (middle of 15-20 range)

# Blink suppression threshold
_BLINK_SUPPRESSION_THRESHOLD = 8.0  # blinks/min — below this indicates suppression


@dataclass(frozen=True)
class BlinkEvent:
    """A single detected blink event."""

    timestamp: float  # When the blink was detected (center of blink)
    duration_frames: int  # Number of frames eyes were closed
    min_ear: float  # Minimum EAR during blink


@dataclass(frozen=True)
class BlinkState:
    """Current blink detection state snapshot."""

    ear_left: float  # Current left eye EAR
    ear_right: float  # Current right eye EAR
    ear_mean: float  # Mean of both eyes
    is_closed: bool  # Currently below threshold
    blink_rate: float | None  # Blinks per minute (rolling 60s)
    blink_rate_delta: float | None  # Change from baseline
    blink_suppression_score: float  # 0-1, higher = more suppression
    blink_count_60s: int  # Blinks in last 60s window
    perclos_60s: float | None  # % frames eyes are closed in rolling window
    mean_blink_duration_ms: float | None  # mean blink duration in milliseconds
    ear_variance: float | None  # EAR variance over rolling window


class BlinkDetector:
    """
    Detects blinks and computes blink rate from FaceMesh landmarks.

    Uses EAR (Eye Aspect Ratio) computed from 6 eye landmarks per eye.
    Tracks blink events in a rolling 60-second window and computes
    blink suppression score relative to a calibrated baseline.

    MediaPipe FaceMesh eye landmark indices (from LandmarksConfig):
        left_eye:  [33, 160, 158, 133, 153, 144]
        right_eye: [362, 385, 387, 263, 373, 380]

    Landmark order maps to EAR formula:
        [outer_corner, upper_outer, upper_inner, inner_corner, lower_inner, lower_outer]
        = [p1, p2, p3, p4, p5, p6]

    Usage:
        detector = BlinkDetector()
        state = detector.update(landmarks_px, timestamp)
        features = detector.get_kinematic_blink_features()
    """

    def __init__(
        self,
        blink_config: BlinkSignalConfig | None = None,
        landmarks_config: LandmarksConfig | None = None,
        baseline_blink_rate: float = _DEFAULT_BASELINE_BLINK_RATE,
        history_window_seconds: float = 60.0,
    ) -> None:
        self._config = blink_config or BlinkSignalConfig()
        self._landmarks = landmarks_config or LandmarksConfig()
        self._baseline_blink_rate = baseline_blink_rate
        self._history_window_s = history_window_seconds

        # Eye landmark indices
        self._left_eye_indices = self._landmarks.left_eye
        self._right_eye_indices = self._landmarks.right_eye

        # Blink detection state machine
        self._closed_frames = 0  # Consecutive frames below threshold
        self._min_ear_during_close = 1.0  # Track minimum EAR during closure
        self._blink_in_progress = False  # Whether we're in a blink

        # Blink event history (rolling window)
        self._blink_events: deque[BlinkEvent] = deque()
        self._ear_history: deque[tuple[float, float]] = deque()
        self._closed_frame_history: deque[tuple[float, int]] = deque()

        # Latest state
        self._latest_state: BlinkState | None = None

    @property
    def latest_state(self) -> BlinkState | None:
        """Most recent blink state."""
        return self._latest_state

    @property
    def baseline_blink_rate(self) -> float:
        """The calibrated baseline blink rate."""
        return self._baseline_blink_rate

    @baseline_blink_rate.setter
    def baseline_blink_rate(self, value: float) -> None:
        """Update the baseline blink rate (e.g., after calibration)."""
        self._baseline_blink_rate = max(1.0, value)

    def personalize_threshold_from_ear_samples(
        self,
        ear_samples: list[float],
        *,
        percentile: float = 0.15,
    ) -> None:
        """Personalize EAR close threshold from calibration samples."""
        if not ear_samples:
            return
        pct = float(np.clip(percentile, 0.05, 0.45))
        thresh = float(np.percentile(np.asarray(ear_samples, dtype=np.float64), pct * 100.0))
        # Keep recovery slightly above threshold.
        self._config.ear_threshold = float(np.clip(thresh, 0.12, 0.30))
        self._config.ear_recovery = max(self._config.ear_threshold + 0.03, self._config.ear_recovery)

    @staticmethod
    def compute_ear(eye_landmarks: NDArray[np.floating]) -> float:
        """
        Compute Eye Aspect Ratio for a single eye.

        EAR = (||p2 - p6|| + ||p3 - p5||) / (2 * ||p1 - p4||)

        Args:
            eye_landmarks: (6, 2) array of pixel coordinates for one eye,
                ordered as [p1, p2, p3, p4, p5, p6].

        Returns:
            EAR value. Typical open eye: 0.25-0.35, closed: < 0.15.
        """
        p1, p2, p3, p4, p5, p6 = eye_landmarks

        # Vertical distances
        v1 = np.linalg.norm(p2 - p6)  # upper-outer to lower-outer
        v2 = np.linalg.norm(p3 - p5)  # upper-inner to lower-inner

        # Horizontal distance
        h = np.linalg.norm(p1 - p4)  # outer corner to inner corner

        if h < 1e-6:
            return 0.0

        ear = float((v1 + v2) / (2.0 * h))
        return ear

    def update(
        self,
        landmarks_px: NDArray[np.floating],
        timestamp: float,
    ) -> BlinkState:
        """
        Update blink detection with new frame landmarks.

        Computes EAR for both eyes, runs the blink state machine,
        and updates the rolling blink rate.

        Args:
            landmarks_px: Full face landmarks in pixel coords, shape (478, 2).
            timestamp: Monotonic timestamp of this frame.

        Returns:
            BlinkState with current EAR, blink rate, and suppression score.
        """
        # Extract eye landmarks
        left_eye = landmarks_px[self._left_eye_indices]
        right_eye = landmarks_px[self._right_eye_indices]

        # Compute EAR for each eye
        ear_left = self.compute_ear(left_eye)
        ear_right = self.compute_ear(right_eye)
        ear_mean = (ear_left + ear_right) / 2.0

        # Run blink state machine
        is_closed = ear_mean < self._config.ear_threshold
        self._update_blink_state_machine(ear_mean, is_closed, timestamp)
        self._ear_history.append((timestamp, ear_mean))
        self._closed_frame_history.append((timestamp, 1 if is_closed else 0))

        # Prune old events outside the history window
        self._prune_events(timestamp)
        self._prune_histories(timestamp)

        # Compute blink rate and suppression
        blink_count = len(self._blink_events)
        elapsed = self._get_tracking_duration(timestamp)

        if elapsed >= 5.0:  # Need at least 5s of data
            blink_rate = blink_count * 60.0 / elapsed
            blink_rate_delta = blink_rate - self._baseline_blink_rate
            suppression = self._compute_suppression_score(blink_rate)
        else:
            blink_rate = None
            blink_rate_delta = None
            suppression = 0.0

        perclos = self._compute_perclos()
        ear_variance = self._compute_ear_variance()
        mean_blink_ms = self._compute_mean_blink_duration_ms()

        state = BlinkState(
            ear_left=ear_left,
            ear_right=ear_right,
            ear_mean=ear_mean,
            is_closed=is_closed,
            blink_rate=blink_rate,
            blink_rate_delta=blink_rate_delta,
            blink_suppression_score=suppression,
            blink_count_60s=blink_count,
            perclos_60s=perclos,
            mean_blink_duration_ms=mean_blink_ms,
            ear_variance=ear_variance,
        )

        self._latest_state = state
        return state

    def _update_blink_state_machine(
        self,
        ear_mean: float,
        is_closed: bool,
        timestamp: float,
    ) -> None:
        """
        Run the blink detection state machine.

        Blink detection requires:
        1. EAR drops below threshold for >= min_frames consecutive frames
        2. EAR recovers above recovery threshold

        This prevents detecting slow eye closure (drowsiness) as rapid blinks.
        """
        if is_closed:
            self._closed_frames += 1
            self._min_ear_during_close = min(self._min_ear_during_close, ear_mean)

            if self._closed_frames >= self._config.min_frames:
                self._blink_in_progress = True

        else:
            # Eyes are open — check for blink completion
            if self._blink_in_progress and ear_mean >= self._config.ear_recovery:
                # Blink completed
                event = BlinkEvent(
                    timestamp=timestamp,
                    duration_frames=self._closed_frames,
                    min_ear=self._min_ear_during_close,
                )
                self._blink_events.append(event)
                logger.debug(
                    f"Blink detected: duration={self._closed_frames} frames, "
                    f"min_ear={self._min_ear_during_close:.3f}"
                )

            # Reset state
            self._closed_frames = 0
            self._min_ear_during_close = 1.0
            self._blink_in_progress = False

    def _prune_events(self, current_time: float) -> None:
        """Remove blink events older than the history window."""
        cutoff = current_time - self._history_window_s
        while self._blink_events and self._blink_events[0].timestamp < cutoff:
            self._blink_events.popleft()

    def _prune_histories(self, current_time: float) -> None:
        cutoff = current_time - self._history_window_s
        while self._ear_history and self._ear_history[0][0] < cutoff:
            self._ear_history.popleft()
        while self._closed_frame_history and self._closed_frame_history[0][0] < cutoff:
            self._closed_frame_history.popleft()

    def _get_tracking_duration(self, current_time: float) -> float:
        """
        Get the effective tracking duration for rate computation.

        Uses the time since the oldest event in the window, capped at
        the history window length.
        """
        if not self._blink_events:
            # No events — use a small estimate to avoid division by zero
            # Return the window size so rate computes to 0
            return self._history_window_s

        oldest = self._blink_events[0].timestamp
        duration = current_time - oldest
        return min(max(duration, 1.0), self._history_window_s)

    def _compute_suppression_score(self, blink_rate: float) -> float:
        """
        Compute blink suppression score (0-1).

        Score increases as blink rate drops below the suppression threshold.
        - 0.0 = normal blink rate (>= threshold)
        - 1.0 = near-zero blinking (complete suppression)

        Uses a linear scale from the suppression threshold to zero.
        """
        if blink_rate >= _BLINK_SUPPRESSION_THRESHOLD:
            return 0.0

        # Linear interpolation: threshold → 0 maps to 0.0 → 1.0
        score = 1.0 - (blink_rate / _BLINK_SUPPRESSION_THRESHOLD)
        return float(np.clip(score, 0.0, 1.0))

    def _compute_perclos(self) -> float | None:
        if not self._closed_frame_history:
            return None
        closed = sum(flag for _, flag in self._closed_frame_history)
        total = len(self._closed_frame_history)
        if total <= 0:
            return None
        return float(np.clip(closed / total, 0.0, 1.0))

    def _compute_ear_variance(self) -> float | None:
        if len(self._ear_history) < 3:
            return None
        vals = np.array([v for _, v in self._ear_history], dtype=np.float64)
        return float(np.var(vals))

    def _compute_mean_blink_duration_ms(self) -> float | None:
        if not self._blink_events:
            return None
        frame_durations = np.array([evt.duration_frames for evt in self._blink_events], dtype=np.float64)
        # Approximate detector frame rate at 30 FPS.
        return float(np.mean(frame_durations) * (1000.0 / 30.0))

    def get_blink_features(self) -> dict[str, float | None]:
        """
        Get blink-related features for KinematicFeatures.

        Returns:
            Dict with blink_rate, blink_rate_delta, blink_suppression_score.
        """
        state = self._latest_state
        if state is None:
            return {
                "blink_rate": None,
                "blink_rate_delta": None,
                "blink_suppression_score": None,
                "perclos_60s": None,
                "mean_blink_duration_ms": None,
                "ear_variance": None,
            }

        return {
            "blink_rate": state.blink_rate,
            "blink_rate_delta": state.blink_rate_delta,
            "blink_suppression_score": state.blink_suppression_score,
            "perclos_60s": state.perclos_60s,
            "mean_blink_duration_ms": state.mean_blink_duration_ms,
            "ear_variance": state.ear_variance,
        }

    def reset(self) -> None:
        """Reset all state."""
        self._closed_frames = 0
        self._min_ear_during_close = 1.0
        self._blink_in_progress = False
        self._blink_events.clear()
        self._ear_history.clear()
        self._closed_frame_history.clear()
        self._latest_state = None
