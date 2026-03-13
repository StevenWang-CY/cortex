"""
State Engine — Parasympathetic Rebound Detection

Detects the optimal learning window that opens immediately after a successful
problem solve.

After the sympathetic stress of working through a hard problem, a correct
submission triggers parasympathetic recovery: heart rate settles back toward
baseline and HRV begins rising.  This "rebound" window is when the brain is
most receptive to consolidating what was just learned — the ideal moment to
surface a spaced-repetition card, suggest a similar problem, or prompt a
brief reflection.

Detection conditions (all must be true simultaneously):
    1. Problem was accepted (``accepted=True``).
    2. Heart rate is within 5% of baseline:  ``|HR - HR_baseline| / HR_baseline < 0.05``
    3. HRV derivative is positive (parasympathetic rising):  ``HRV_current > HRV_prev``
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_HR_PROXIMITY_THRESHOLD = 0.05  # 5% of baseline


class ParasympatheticReboundDetector:
    """
    Detects parasympathetic rebound — the optimal learning window after a
    successful solve, when heart rate has returned near baseline and HRV
    is rising.

    Usage::

        detector = ParasympatheticReboundDetector()
        if detector.update(accepted=True, hr=72.0, hr_baseline=70.0,
                           hrv_current=55.0, hrv_prev=48.0):
            show_reflection_prompt()
    """

    def __init__(self) -> None:
        self._latest_rebound: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(
        self,
        accepted: bool,
        hr: float | None,
        hr_baseline: float,
        hrv_current: float | None,
        hrv_prev: float | None,
    ) -> bool:
        """
        Evaluate whether parasympathetic rebound conditions are met.

        Args:
            accepted: Whether the problem was just accepted (AC).
            hr: Current heart rate in BPM.  ``None`` if sensor unavailable.
            hr_baseline: Resting / session-baseline heart rate in BPM.
            hrv_current: Current HRV RMSSD in milliseconds.  ``None`` if
                         unavailable.
            hrv_prev: Previous HRV RMSSD sample in milliseconds.  ``None``
                      if unavailable.

        Returns:
            ``True`` if all three rebound conditions are satisfied.
        """
        # Condition 1: problem accepted
        if not accepted:
            self._latest_rebound = False
            return False

        # Condition 2: HR within 5% of baseline
        if hr is None or hr_baseline <= 0:
            self._latest_rebound = False
            return False

        hr_deviation = abs(hr - hr_baseline) / hr_baseline
        if hr_deviation >= _HR_PROXIMITY_THRESHOLD:
            self._latest_rebound = False
            return False

        # Condition 3: HRV derivative positive (parasympathetic rising)
        if hrv_current is None or hrv_prev is None:
            self._latest_rebound = False
            return False

        if hrv_current < hrv_prev:
            self._latest_rebound = False
            return False

        # All conditions met
        self._latest_rebound = True
        logger.info(
            "Parasympathetic rebound detected: HR=%.1f (baseline=%.1f, "
            "dev=%.1f%%), HRV rising %.1f→%.1f",
            hr,
            hr_baseline,
            hr_deviation * 100,
            hrv_prev,
            hrv_current,
        )
        return True

    def is_rebounding(self) -> bool:
        """Return True if the last update detected a parasympathetic rebound."""
        return self._latest_rebound

    def reset(self) -> None:
        """Clear detection state."""
        self._latest_rebound = False
