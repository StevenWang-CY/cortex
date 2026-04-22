"""
State Engine — Stress Integral Tracker (Biological Pomodoros)

Replaces static 25-minute timers with biology-driven break detection.
Calculates cumulative stress load L by integrating HRV suppression:

    L = integral(max(0, HRV_base - HRV(t)) dt)

When L crosses a dynamic personal threshold, a break intervention is emitted.
The user can ride deep FLOW indefinitely until their actual biology flags fatigue.

The threshold is dynamically adjusted by the longitudinal tracker's
sensitivity_multiplier based on multi-day trends.
"""

from __future__ import annotations

import logging
import time
from collections import deque

logger = logging.getLogger(__name__)

# Default threshold: 500 ms*s of cumulative HRV suppression
# At baseline HRV=50ms and current HRV=30ms: suppression=20ms/sample
# At 2Hz sampling: ~25 minutes to reach 500 ms*s threshold
_DEFAULT_THRESHOLD = 500.0
_MAX_HISTORY = 7200  # 1 hour at 2Hz


class StressIntegralTracker:
    """
    Tracks cumulative stress load via HRV suppression integration.

    The integral L accumulates whenever HRV drops below baseline:
        dL = max(0, hrv_baseline - hrv_current) * dt

    Break is recommended when L exceeds a threshold that adapts
    based on the longitudinal model's sensitivity multiplier.

    Usage:
        tracker = StressIntegralTracker(hrv_baseline=50.0)
        tracker.update(hrv_rmssd=35.0, timestamp=now)
        if tracker.should_break():
            emit_break_intervention()
        tracker.reset()  # After break taken
    """

    def __init__(
        self,
        hrv_baseline: float = 50.0,
        hrv_sigma: float = 1.0,
        threshold: float = _DEFAULT_THRESHOLD,
        sensitivity_multiplier: float = 1.0,
    ) -> None:
        self._hrv_baseline = hrv_baseline
        self._hrv_sigma = max(1.0, hrv_sigma)
        self._base_threshold = threshold
        self._sensitivity_multiplier = sensitivity_multiplier
        self._integral: float = 0.0
        self._last_timestamp: float | None = None
        self._last_hrv: float | None = None
        self._break_emitted: bool = False
        self._warning_emitted: bool = False
        self._history: deque[tuple[float, float]] = deque(maxlen=_MAX_HISTORY)

    @property
    def current_load(self) -> float:
        """Current cumulative stress load L."""
        return self._integral

    @property
    def threshold(self) -> float:
        """Current dynamic break threshold."""
        return self._base_threshold * self._sensitivity_multiplier

    @property
    def load_ratio(self) -> float:
        """Ratio of current load to threshold (0-1+)."""
        t = self.threshold
        if t <= 0:
            return 0.0
        return self._integral / t

    def update_baseline(self, hrv_baseline: float) -> None:
        """Update the HRV baseline (e.g., from longitudinal tracker)."""
        self._hrv_baseline = hrv_baseline

    def update_sigma(self, hrv_sigma: float) -> None:
        """Update personalized HRV dispersion for standardized deficit."""
        self._hrv_sigma = max(1.0, hrv_sigma)

    def update_sensitivity(self, multiplier: float) -> None:
        """Update the sensitivity multiplier from longitudinal tracker."""
        self._sensitivity_multiplier = max(0.5, min(2.0, multiplier))

    def update(self, hrv_rmssd: float | None, timestamp: float | None = None) -> float:
        """
        Update the stress integral with a new HRV measurement.

        Uses trapezoidal numerical integration for accuracy.

        Args:
            hrv_rmssd: Current HRV (RMSSD) in milliseconds. None = skip.
            timestamp: Monotonic timestamp. None = use time.monotonic().

        Returns:
            Current cumulative stress load L.
        """
        if hrv_rmssd is None:
            return self._integral

        if timestamp is None:
            timestamp = time.monotonic()

        if self._last_timestamp is not None and self._last_hrv is not None:
            dt = timestamp - self._last_timestamp
            if dt > 0 and dt < 30.0:  # Ignore gaps > 30s (pauses, etc.)
                # Trapezoidal integration of suppression
                suppression_now = max(0.0, (self._hrv_baseline - hrv_rmssd) / self._hrv_sigma)
                suppression_prev = max(0.0, (self._hrv_baseline - self._last_hrv) / self._hrv_sigma)
                avg_suppression = (suppression_now + suppression_prev) / 2.0
                self._integral += avg_suppression * dt

        self._last_timestamp = timestamp
        self._last_hrv = hrv_rmssd
        self._history.append((timestamp, self._integral))

        return self._integral

    def should_warn(self) -> bool:
        """
        Check if cumulative stress is approaching break threshold (80%).

        Returns True once per threshold approach until reset().
        Allows the intervention pipeline to surface a "getting tired" hint.
        """
        if self._warning_emitted:
            return False
        if self.load_ratio >= 0.8:
            self._warning_emitted = True
            logger.info(
                "Stress integral %.1f at %.0f%% of threshold %.1f — pre-break warning",
                self._integral, self.load_ratio * 100, self.threshold,
            )
            return True
        return False

    def should_break(self) -> bool:
        """
        Check if cumulative stress warrants a break.

        Only returns True once per threshold crossing until reset().
        """
        if self._break_emitted:
            return False
        if self._integral >= self.threshold:
            self._break_emitted = True
            logger.info(
                "Stress integral %.1f crossed threshold %.1f (sensitivity=%.2f)",
                self._integral, self.threshold, self._sensitivity_multiplier,
            )
            return True
        return False

    def reset(self) -> None:
        """Reset the stress integral after a break is taken."""
        self._integral = 0.0
        self._break_emitted = False
        self._warning_emitted = False
        self._last_timestamp = None
        self._last_hrv = None
        self._history.clear()
        logger.info("Stress integral reset after break")

    def apply_recovery_credit(self, seconds: float = 120.0) -> None:
        """
        Apply recovery credit after a confirmed restorative action.

        HEURISTIC: subtract equivalent of sustained low-deficit time.
        """
        credit = max(0.0, seconds)
        self._integral = max(0.0, self._integral - credit)

    def get_history(self, window_seconds: float = 3600.0) -> list[tuple[float, float]]:
        """
        Get stress integral history for visualization.

        Args:
            window_seconds: How far back to look.

        Returns:
            List of (timestamp, integral_value) pairs.
        """
        if not self._history:
            return []
        cutoff = self._history[-1][0] - window_seconds
        return [(t, v) for t, v in self._history if t >= cutoff]

    def to_dict(self) -> dict:
        """Serialize current state for Redis persistence."""
        return {
            "integral": self._integral,
            "hrv_baseline": self._hrv_baseline,
            "hrv_sigma": self._hrv_sigma,
            "base_threshold": self._base_threshold,
            "sensitivity_multiplier": self._sensitivity_multiplier,
            "break_emitted": self._break_emitted,
            "last_timestamp": self._last_timestamp,
            "last_hrv": self._last_hrv,
        }

    @classmethod
    def from_dict(cls, data: dict) -> StressIntegralTracker:
        """Restore from serialized state."""
        tracker = cls(
            hrv_baseline=data.get("hrv_baseline", 50.0),
            hrv_sigma=data.get("hrv_sigma", 10.0),
            threshold=data.get("base_threshold", _DEFAULT_THRESHOLD),
            sensitivity_multiplier=data.get("sensitivity_multiplier", 1.0),
        )
        tracker._integral = data.get("integral", 0.0)
        tracker._break_emitted = data.get("break_emitted", False)
        tracker._last_timestamp = data.get("last_timestamp")
        tracker._last_hrv = data.get("last_hrv")
        return tracker
