"""Elapsed-time blink and eye-closure measurements.

Only intervals bounded by valid eye observations contribute exposure. This
makes blink rate, PERCLOS, and blink duration independent of camera FPS and
prevents a missing camera from looking like unusually steady open eyes.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from cortex.libs.config.settings import BlinkSignalConfig, LandmarksConfig

logger = logging.getLogger(__name__)

_DEFAULT_BASELINE_BLINK_RATE = 17.0
_BLINK_SUPPRESSION_THRESHOLD = 8.0


@dataclass(frozen=True)
class BlinkEvent:
    """One blink delimited by monotonic observations."""

    start_mono_seconds: float
    end_mono_seconds: float
    duration_ms: float
    min_ear: float

    @property
    def timestamp(self) -> float:
        """Compatibility alias for the event completion time."""

        return self.end_mono_seconds


@dataclass(frozen=True)
class BlinkState:
    """Latest elapsed-time eye measurement."""

    ear_left: float
    ear_right: float
    ear_mean: float
    is_closed: bool
    blink_rate: float | None
    blink_rate_delta: float | None
    blink_suppression_score: float | None
    blink_count_60s: int
    perclos_60s: float | None
    mean_blink_duration_ms: float | None
    ear_variance: float | None
    valid_exposure_seconds: float
    closed_exposure_seconds: float
    readiness: str


@dataclass(frozen=True)
class _ExposureInterval:
    start: float
    end: float
    closed: bool
    ear: float

    @property
    def duration(self) -> float:
        return self.end - self.start


class BlinkDetector:
    """Detect blinks and derive rates from valid monotonic exposure."""

    def __init__(
        self,
        blink_config: BlinkSignalConfig | None = None,
        landmarks_config: LandmarksConfig | None = None,
        baseline_blink_rate: float = _DEFAULT_BASELINE_BLINK_RATE,
        history_window_seconds: float | None = None,
    ) -> None:
        self._config = blink_config or BlinkSignalConfig()
        self._landmarks = landmarks_config or LandmarksConfig()
        self._baseline_blink_rate = max(1.0, float(baseline_blink_rate))
        self._history_window_s = float(
            history_window_seconds
            if history_window_seconds is not None
            else self._config.history_window_seconds
        )
        if self._history_window_s <= 0:
            raise ValueError("blink history window must be positive")

        self._left_eye_indices = self._landmarks.left_eye
        self._right_eye_indices = self._landmarks.right_eye
        self._blink_events: deque[BlinkEvent] = deque()
        self._exposure: deque[_ExposureInterval] = deque()

        self._last_timestamp: float | None = None
        self._last_closed: bool | None = None
        self._last_ear: float | None = None
        self._closure_started_at: float | None = None
        self._closure_min_ear = 1.0
        self._closure_had_invalid_gap = False
        self._latest_state: BlinkState | None = None

    @property
    def latest_state(self) -> BlinkState | None:
        return self._latest_state

    @property
    def baseline_blink_rate(self) -> float:
        return self._baseline_blink_rate

    @baseline_blink_rate.setter
    def baseline_blink_rate(self, value: float) -> None:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("baseline blink rate must be finite and positive")
        self._baseline_blink_rate = max(1.0, float(value))

    @staticmethod
    def compute_ear(eye_landmarks: NDArray[np.floating]) -> float:
        points = np.asarray(eye_landmarks, dtype=np.float64)
        if points.shape != (6, 2) or not bool(np.isfinite(points).all()):
            raise ValueError("eye landmarks must be a finite (6, 2) array")
        p1, p2, p3, p4, p5, p6 = points
        vertical_1 = np.linalg.norm(p2 - p6)
        vertical_2 = np.linalg.norm(p3 - p5)
        horizontal = np.linalg.norm(p1 - p4)
        if horizontal < 1e-6:
            return 0.0
        return float((vertical_1 + vertical_2) / (2.0 * horizontal))

    def update(
        self,
        landmarks_px: NDArray[np.floating],
        timestamp: float,
    ) -> BlinkState:
        """Consume one valid eye-visible observation at monotonic seconds."""

        now = self._validate_time(timestamp)
        landmarks = np.asarray(landmarks_px)
        left = landmarks[self._left_eye_indices]
        right = landmarks[self._right_eye_indices]
        ear_left = self.compute_ear(left)
        ear_right = self.compute_ear(right)
        ear_mean = (ear_left + ear_right) / 2.0

        raw_closed = ear_mean < self._config.ear_threshold
        if self._closure_started_at is not None and ear_mean < self._config.ear_recovery:
            is_closed = True
        else:
            is_closed = raw_closed

        self._record_exposure(now)
        self._advance_blink(ear_mean, is_closed, now)
        self._last_timestamp = now
        self._last_closed = is_closed
        self._last_ear = ear_mean
        self._prune(now)

        valid_exposure = sum(interval.duration for interval in self._exposure)
        closed_exposure = sum(
            interval.duration for interval in self._exposure if interval.closed
        )
        ready = valid_exposure >= self._config.min_valid_exposure_seconds
        blink_count = len(self._blink_events)
        if ready and valid_exposure > 0:
            blink_rate = blink_count * 60.0 / valid_exposure
            blink_delta = blink_rate - self._baseline_blink_rate
            suppression = self._compute_suppression_score(blink_rate)
            perclos = float(np.clip(closed_exposure / valid_exposure, 0.0, 1.0))
            readiness = "ready"
        else:
            blink_rate = None
            blink_delta = None
            suppression = None
            perclos = None
            readiness = "warming_up"

        state = BlinkState(
            ear_left=ear_left,
            ear_right=ear_right,
            ear_mean=ear_mean,
            is_closed=is_closed,
            blink_rate=blink_rate,
            blink_rate_delta=blink_delta,
            blink_suppression_score=suppression,
            blink_count_60s=blink_count,
            perclos_60s=perclos,
            mean_blink_duration_ms=self._mean_blink_duration_ms(),
            ear_variance=self._time_weighted_ear_variance(),
            valid_exposure_seconds=valid_exposure,
            closed_exposure_seconds=closed_exposure,
            readiness=readiness,
        )
        self._latest_state = state
        return state

    def observe_missing(self, timestamp: float) -> None:
        """Advance time without adding eye-visible exposure."""

        now = self._validate_time(timestamp)
        if self._closure_started_at is not None:
            self._closure_had_invalid_gap = True
        self._last_timestamp = now
        self._last_closed = None
        self._last_ear = None
        self._prune(now)

    def _validate_time(self, timestamp: float) -> float:
        now = float(timestamp)
        if not math.isfinite(now) or now < 0:
            raise ValueError("blink timestamp must be finite and non-negative")
        if self._last_timestamp is not None and now <= self._last_timestamp:
            raise ValueError("blink timestamps must be strictly increasing")
        return now

    def _record_exposure(self, now: float) -> None:
        if (
            self._last_timestamp is None
            or self._last_closed is None
            or self._last_ear is None
        ):
            return
        gap = now - self._last_timestamp
        if gap * 1000.0 > self._config.max_valid_gap_ms:
            if self._closure_started_at is not None:
                self._closure_had_invalid_gap = True
            return
        self._exposure.append(
            _ExposureInterval(
                start=self._last_timestamp,
                end=now,
                closed=self._last_closed,
                ear=self._last_ear,
            )
        )

    def _advance_blink(self, ear: float, is_closed: bool, now: float) -> None:
        if is_closed:
            if self._closure_started_at is None:
                self._closure_started_at = now
                self._closure_min_ear = ear
                self._closure_had_invalid_gap = False
            else:
                self._closure_min_ear = min(self._closure_min_ear, ear)
            return

        if self._closure_started_at is None or ear < self._config.ear_recovery:
            return
        duration_ms = (now - self._closure_started_at) * 1000.0
        if (
            not self._closure_had_invalid_gap
            and self._config.min_closed_ms <= duration_ms <= self._config.max_closed_ms
        ):
            self._blink_events.append(
                BlinkEvent(
                    start_mono_seconds=self._closure_started_at,
                    end_mono_seconds=now,
                    duration_ms=duration_ms,
                    min_ear=self._closure_min_ear,
                )
            )
            logger.debug(
                "Blink detected: duration_ms=%.1f min_ear=%.3f",
                duration_ms,
                self._closure_min_ear,
            )
        self._closure_started_at = None
        self._closure_min_ear = 1.0
        self._closure_had_invalid_gap = False

    def _prune(self, now: float) -> None:
        cutoff = now - self._history_window_s
        while self._blink_events and self._blink_events[0].end_mono_seconds < cutoff:
            self._blink_events.popleft()
        while self._exposure and self._exposure[0].end <= cutoff:
            self._exposure.popleft()
        if self._exposure and self._exposure[0].start < cutoff:
            first = self._exposure.popleft()
            self._exposure.appendleft(
                _ExposureInterval(
                    start=cutoff,
                    end=first.end,
                    closed=first.closed,
                    ear=first.ear,
                )
            )

    @staticmethod
    def _compute_suppression_score(blink_rate: float) -> float:
        if blink_rate >= _BLINK_SUPPRESSION_THRESHOLD:
            return 0.0
        return float(
            np.clip(1.0 - blink_rate / _BLINK_SUPPRESSION_THRESHOLD, 0.0, 1.0)
        )

    def _time_weighted_ear_variance(self) -> float | None:
        if len(self._exposure) < 2:
            return None
        values = np.asarray([item.ear for item in self._exposure], dtype=np.float64)
        weights = np.asarray(
            [item.duration for item in self._exposure], dtype=np.float64
        )
        total = float(np.sum(weights))
        if total <= 0:
            return None
        mean = float(np.sum(values * weights) / total)
        return float(np.sum(weights * (values - mean) ** 2) / total)

    def _mean_blink_duration_ms(self) -> float | None:
        if not self._blink_events:
            return None
        return float(np.mean([event.duration_ms for event in self._blink_events]))

    def get_blink_features(self) -> dict[str, float | None]:
        state = self._latest_state
        if state is None:
            return {
                "blink_rate": None,
                "blink_rate_delta": None,
                "blink_suppression_score": None,
                "perclos_60s": None,
                "mean_blink_duration_ms": None,
                "ear_variance": None,
                "blink_valid_exposure_seconds": None,
            }
        return {
            "blink_rate": state.blink_rate,
            "blink_rate_delta": state.blink_rate_delta,
            "blink_suppression_score": state.blink_suppression_score,
            "perclos_60s": state.perclos_60s,
            "mean_blink_duration_ms": state.mean_blink_duration_ms,
            "ear_variance": state.ear_variance,
            "blink_valid_exposure_seconds": state.valid_exposure_seconds,
        }

    def reset(self) -> None:
        self._blink_events.clear()
        self._exposure.clear()
        self._last_timestamp = None
        self._last_closed = None
        self._last_ear = None
        self._closure_started_at = None
        self._closure_min_ear = 1.0
        self._closure_had_invalid_gap = False
        self._latest_state = None
