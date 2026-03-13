"""
State Engine — Destructive Struggle Detection

Identifies when a user's struggle has crossed from productive to destructive
during a LeetCode session.  Two independent pathways are monitored:

**Comprehension failure**
    The user keeps re-reading the problem statement (reread_count > 2) while
    allostatic load is rising and they have been stuck on the same stage for
    over 5 minutes.  This signals a conceptual block — they do not yet
    understand *what* the problem is asking.

**Implementation thrash**
    The user has received > 2 Wrong Answers within 10 minutes AND their
    code-delete ratio over the last 60 seconds exceeds 0.5 (more deleting
    than writing) AND their HRV RMSSD has dropped below 80% of baseline
    (sympathetic dominance).  This signals they are rewriting blindly.

The detector outputs a ``DestructiveStruggleEstimate`` pydantic model.
"""

from __future__ import annotations

import logging
import time

from cortex.libs.schemas.leetcode import DestructiveStruggleEstimate

logger = logging.getLogger(__name__)

# Comprehension-failure thresholds
_REREAD_THRESHOLD = 2
_STAGE_DWELL_THRESHOLD_S = 300.0  # 5 minutes

# Implementation-thrash thresholds
_WA_COUNT_THRESHOLD = 2
_WA_WINDOW_S = 600.0  # 10 minutes
_CODE_DELETE_RATIO_THRESHOLD = 0.5
_HRV_DROP_RATIO = 0.80  # HRV below 80% of baseline


class DestructiveStruggleDetector:
    """
    Detects destructive struggle via two complementary pathways.

    Usage::

        detector = DestructiveStruggleDetector()
        estimate = detector.update(
            reread_count=3,
            wrong_answer_count=0,
            code_delete_ratio=0.1,
            stage_dwell_s=320.0,
            allostatic_load=0.7,
            allostatic_load_prev=0.5,
            hrv_rmssd=45.0,
            hrv_baseline=60.0,
            wa_timestamps=[t1, t2, t3],
        )
        if estimate.is_destructive:
            intervene(estimate.pathway)
    """

    def __init__(self) -> None:
        self._latest: DestructiveStruggleEstimate = DestructiveStruggleEstimate()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(
        self,
        reread_count: int,
        wrong_answer_count: int,
        code_delete_ratio: float,
        stage_dwell_s: float,
        allostatic_load: float,
        allostatic_load_prev: float,
        hrv_rmssd: float,
        hrv_baseline: float,
        wa_timestamps: list[float],
        current_time: float | None = None,
    ) -> DestructiveStruggleEstimate:
        """
        Evaluate both destructive-struggle pathways and return an estimate.

        Args:
            reread_count: Number of times the problem statement has been
                          re-read in the current session.
            wrong_answer_count: Total Wrong Answer submissions so far.
            code_delete_ratio: Ratio of deleted characters to total characters
                               typed over the last 60 seconds.  1.0 = pure
                               deletion.
            stage_dwell_s: Seconds spent on the current cognitive stage.
            allostatic_load: Current allostatic-load score.
            allostatic_load_prev: Previous allostatic-load score (for trend).
            hrv_rmssd: Current HRV RMSSD in milliseconds.
            hrv_baseline: Baseline HRV RMSSD in milliseconds.
            wa_timestamps: Monotonic timestamps of all Wrong Answer
                           submissions in the session.
            current_time: Override for the current monotonic clock.  Defaults
                          to ``time.monotonic()``.

        Returns:
            A ``DestructiveStruggleEstimate`` with detection result.
        """
        if current_time is None:
            current_time = time.monotonic()

        # --- Pathway 1: Comprehension failure ---
        comp = self._check_comprehension(
            reread_count=reread_count,
            allostatic_load=allostatic_load,
            allostatic_load_prev=allostatic_load_prev,
            stage_dwell_s=stage_dwell_s,
        )

        # --- Pathway 2: Implementation thrash ---
        impl = self._check_implementation(
            wrong_answer_count=wrong_answer_count,
            code_delete_ratio=code_delete_ratio,
            hrv_rmssd=hrv_rmssd,
            hrv_baseline=hrv_baseline,
            wa_timestamps=wa_timestamps,
            current_time=current_time,
        )

        # Pick the pathway with higher confidence (or neither)
        if comp > 0.0 or impl > 0.0:
            if comp >= impl:
                pathway, confidence = "comprehension", comp
            else:
                pathway, confidence = "implementation", impl

            self._latest = DestructiveStruggleEstimate(
                is_destructive=True,
                pathway=pathway,
                confidence=round(min(confidence, 1.0), 3),
            )
            logger.info(
                "Destructive struggle detected: pathway=%s confidence=%.3f",
                pathway,
                confidence,
            )
        else:
            self._latest = DestructiveStruggleEstimate()

        return self._latest

    def reset(self) -> None:
        """Clear detection state."""
        self._latest = DestructiveStruggleEstimate()

    # ------------------------------------------------------------------
    # Internal pathway checks
    # ------------------------------------------------------------------

    @staticmethod
    def _check_comprehension(
        reread_count: int,
        allostatic_load: float,
        allostatic_load_prev: float,
        stage_dwell_s: float,
    ) -> float:
        """
        Comprehension-failure pathway.

        Returns a confidence score in [0, 1].  Zero means pathway not triggered.
        """
        if reread_count <= _REREAD_THRESHOLD:
            return 0.0
        if allostatic_load <= allostatic_load_prev:
            return 0.0
        if stage_dwell_s <= _STAGE_DWELL_THRESHOLD_S:
            return 0.0

        # All conditions met — confidence scales with reread count and dwell
        reread_factor = min((reread_count - _REREAD_THRESHOLD) / 3.0, 1.0)
        dwell_factor = min(
            (stage_dwell_s - _STAGE_DWELL_THRESHOLD_S) / _STAGE_DWELL_THRESHOLD_S,
            1.0,
        )
        load_delta = allostatic_load - allostatic_load_prev
        load_factor = min(load_delta / 0.3, 1.0)

        return 0.4 * reread_factor + 0.3 * dwell_factor + 0.3 * load_factor

    @staticmethod
    def _check_implementation(
        wrong_answer_count: int,
        code_delete_ratio: float,
        hrv_rmssd: float,
        hrv_baseline: float,
        wa_timestamps: list[float],
        current_time: float,
    ) -> float:
        """
        Implementation-thrash pathway.

        Returns a confidence score in [0, 1].  Zero means pathway not triggered.
        """
        # Count WAs within the last 10-minute window
        wa_cutoff = current_time - _WA_WINDOW_S
        recent_wa = sum(1 for t in wa_timestamps if t >= wa_cutoff)

        if recent_wa <= _WA_COUNT_THRESHOLD:
            return 0.0
        if code_delete_ratio <= _CODE_DELETE_RATIO_THRESHOLD:
            return 0.0
        if hrv_baseline <= 0 or hrv_rmssd < 0:
            return 0.0
        if hrv_rmssd >= hrv_baseline * _HRV_DROP_RATIO:
            return 0.0

        # All conditions met — compute confidence
        wa_factor = min((recent_wa - _WA_COUNT_THRESHOLD) / 3.0, 1.0)
        delete_factor = min(
            (code_delete_ratio - _CODE_DELETE_RATIO_THRESHOLD) / 0.5, 1.0
        )
        hrv_ratio = hrv_rmssd / hrv_baseline
        hrv_factor = min((_HRV_DROP_RATIO - hrv_ratio) / 0.3, 1.0)

        return 0.4 * wa_factor + 0.3 * delete_factor + 0.3 * hrv_factor
