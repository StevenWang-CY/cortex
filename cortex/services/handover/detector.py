"""
Handover — Shutdown Detector

Detects when the user should stop working based on compound signals:
- Sustained posture collapse (slumping)
- Dropping HRV (physiological fatigue)
- Rising syntax errors (declining code quality)
- Late hour (after 10 PM by default)

When all conditions are met, triggers the handover workflow.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

from cortex.libs.config.settings import HandoverConfig

logger = logging.getLogger(__name__)

# Detection thresholds
_DEFAULT_LATE_HOUR = 23  # 11 PM
_POSTURE_SLUMP_THRESHOLD = 0.6
_HRV_DROP_THRESHOLD = 0.7  # HRV at 70% of baseline
_ERROR_RATE_THRESHOLD = 3  # errors per 5 minutes
_MIN_DURATION_SECONDS = 300.0  # 5 minutes sustained
_COOLDOWN_SECONDS = 3600.0  # 1 hour cooldown


class ShutdownDetector:
    """
    Detects when the user should stop working and triggers handover.

    Monitors compound physiological + behavioral signals that indicate
    the user is too fatigued to continue productively.

    Usage:
        detector = ShutdownDetector(hrv_baseline=50.0)
        if detector.should_handover(posture_slump=0.7, hrv=35.0, error_count=5):
            trigger_handover_workflow()
    """

    def __init__(
        self,
        hrv_baseline: float = 50.0,
        late_hour: int | None = None,
        cooldown: float = _COOLDOWN_SECONDS,
        config: HandoverConfig | None = None,
        posture_slump_threshold: float | None = None,
        hrv_drop_threshold: float | None = None,
        error_rate_threshold: int | None = None,
    ) -> None:
        self._hrv_baseline = hrv_baseline
        cfg = config or HandoverConfig()
        self._late_hour = late_hour if late_hour is not None else cfg.late_hour
        self._posture_slump_threshold = (
            posture_slump_threshold if posture_slump_threshold is not None
            else cfg.posture_slump_threshold
        )
        self._hrv_drop_threshold = (
            hrv_drop_threshold if hrv_drop_threshold is not None
            else cfg.hrv_drop_threshold
        )
        self._error_rate_threshold = (
            error_rate_threshold if error_rate_threshold is not None
            else cfg.error_rate_threshold
        )
        self._cooldown = cooldown
        self._fatigue_start: float | None = None
        self._last_trigger: float = 0.0
        self._error_timestamps: list[float] = []

    def update_baseline(self, hrv_baseline: float) -> None:
        """Update HRV baseline."""
        self._hrv_baseline = hrv_baseline

    def record_error(self, timestamp: float | None = None) -> None:
        """Record a syntax/compile error occurrence."""
        ts = timestamp or time.monotonic()
        self._error_timestamps.append(ts)
        # Keep only last 5 minutes
        cutoff = ts - 300.0
        self._error_timestamps = [t for t in self._error_timestamps if t >= cutoff]

    def should_handover(
        self,
        posture_slump: float = 0.0,
        hrv: float | None = None,
        error_count: int | None = None,
        current_time: float | None = None,
    ) -> bool:
        """
        Check if handover conditions are met.

        All conditions must be sustained for MIN_DURATION_SECONDS.

        Args:
            posture_slump: Current posture slump score (0-1).
            hrv: Current HRV (RMSSD) in ms.
            error_count: Number of errors in last 5 minutes (optional, uses internal counter).
            current_time: Override timestamp.

        Returns:
            True if handover should be triggered.
        """
        if current_time is None:
            current_time = time.monotonic()

        # Check cooldown
        if current_time - self._last_trigger < self._cooldown:
            self._fatigue_start = None
            return False

        # Check time of day
        now = datetime.now()
        is_late = now.hour >= self._late_hour or now.hour < 5

        if not is_late:
            self._fatigue_start = None
            return False

        # Check posture collapse
        has_posture_collapse = posture_slump >= self._posture_slump_threshold

        # Check HRV dropping
        has_hrv_drop = False
        if hrv is not None:
            hrv_ratio = hrv / self._hrv_baseline if self._hrv_baseline > 0 else 1.0
            has_hrv_drop = hrv_ratio < self._hrv_drop_threshold

        # Check error rate
        if error_count is None:
            error_count = len(self._error_timestamps)
        has_errors = error_count >= self._error_rate_threshold

        # Need at least 2 of the 3 physiological/behavioral signals
        signals_met = sum([has_posture_collapse, has_hrv_drop, has_errors])
        if signals_met < 2:
            self._fatigue_start = None
            return False

        # Track duration
        if self._fatigue_start is None:
            self._fatigue_start = current_time
            return False

        duration = current_time - self._fatigue_start
        if duration >= _MIN_DURATION_SECONDS:
            self._last_trigger = current_time
            self._fatigue_start = None
            logger.info(
                "Shutdown detected: posture=%.2f, hrv=%s, errors=%d, duration=%.0fs",
                posture_slump, hrv, error_count, duration,
            )
            return True

        return False

    @property
    def is_accumulating(self) -> bool:
        """Whether fatigue conditions are being tracked."""
        return self._fatigue_start is not None
