"""
LeetCode Mode Resolver

Maps the generic StateEstimate + LeetCode domain context + biological detector
outputs into a LeetCodeModeEstimate suitable for intervention matrix lookup.

Priority order: AMYGDALA_HIJACK > DESTRUCTIVE_STRUGGLE > FATIGUE > others.
"""

from __future__ import annotations

import logging

from cortex.libs.schemas.leetcode import (
    DestructiveStruggleEstimate,
    LeetCodeContext,
    LeetCodeMode,
    LeetCodeModeEstimate,
    LeetCodeStage,
)
from cortex.libs.schemas.state import StateEstimate

logger = logging.getLogger(__name__)


class LeetCodeModeResolver:
    """Resolve a domain-specific LeetCodeModeEstimate from generic signals.

    Combines the generic cognitive state classification (FLOW / HYPO / HYPER /
    RECOVERY) with amygdala-hijack, destructive-struggle, and parasympathetic-
    rebound detector outputs to produce the final LeetCode biological mode.

    Parameters
    ----------
    aai_threshold:
        Amygdala Activation Index score above which we classify
        AMYGDALA_HIJACK when the generic state is HYPER.
    fatigue_load_threshold:
        Cumulative stress-integral value above which HYPO is upgraded
        to FATIGUE rather than PRODUCTIVE_STRUGGLE.
    """

    def __init__(
        self,
        aai_threshold: float = 0.7,
        fatigue_load_threshold: float = 400.0,
    ) -> None:
        self.aai_threshold = aai_threshold
        self.fatigue_load_threshold = fatigue_load_threshold

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve(
        self,
        state_estimate: StateEstimate,
        leetcode_ctx: LeetCodeContext,
        aai_score: float,
        destructive: DestructiveStruggleEstimate,
        parasympathetic_rebound: bool,
    ) -> LeetCodeModeEstimate:
        """Map generic state + detector outputs to a LeetCodeModeEstimate.

        Parameters
        ----------
        state_estimate:
            Generic cognitive state produced by the state engine every 500 ms.
        leetcode_ctx:
            DOM-derived problem context (stage, submissions, etc.).
        aai_score:
            Amygdala Activation Index from ``AmygdalaHijackDetector``.
            Values above ``aai_threshold`` indicate hijack.
        destructive:
            Output from the destructive-struggle detector.
        parasympathetic_rebound:
            Whether the parasympathetic-rebound detector has fired
            (indicates an optimal learning window).

        Returns
        -------
        LeetCodeModeEstimate
            Combined (stage, mode) estimate ready for intervention lookup.
        """
        generic = state_estimate.state
        stress_integral = state_estimate.stress_integral or 0.0

        mode = self._resolve_mode(
            generic=generic,
            aai_score=aai_score,
            destructive=destructive,
            parasympathetic_rebound=parasympathetic_rebound,
            stress_integral=stress_integral,
            leetcode_ctx=leetcode_ctx,
        )

        rebound_flag = (
            generic == "RECOVERY"
            and parasympathetic_rebound
            and mode == LeetCodeMode.FLOW
        )

        confidence = self._compute_confidence(
            state_confidence=state_estimate.confidence,
            destructive=destructive,
            mode=mode,
            aai_score=aai_score,
        )

        estimate = LeetCodeModeEstimate(
            mode=mode,
            stage=leetcode_ctx.stage,
            confidence=confidence,
            aai_score=aai_score,
            allostatic_load=stress_integral,
            parasympathetic_rebound=rebound_flag,
            destructive=destructive,
        )

        logger.debug(
            "resolved %s + %s → mode=%s  confidence=%.2f  rebound=%s",
            generic,
            leetcode_ctx.stage.value,
            mode.value,
            confidence,
            rebound_flag,
        )

        return estimate

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_mode(
        self,
        generic: str,
        aai_score: float,
        destructive: DestructiveStruggleEstimate,
        parasympathetic_rebound: bool,
        stress_integral: float,
        leetcode_ctx: LeetCodeContext | None = None,
    ) -> LeetCodeMode:
        """Apply the priority-ordered mapping rules.

        Priority: PANIC > AMYGDALA_HIJACK > DESTRUCTIVE_STRUGGLE > FATIGUE > others.

        PANIC is triggered when the user shows extreme distress: rapid Wrong
        Answers (>= 4) combined with high allostatic load (>= fatigue threshold)
        while in HYPER state.  This is more severe than amygdala hijack — the
        user is spiralling.
        """
        if generic == "FLOW":
            return LeetCodeMode.FLOW

        if generic == "HYPER":
            # Highest priority: PANIC — rapid WA + high stress
            if leetcode_ctx is not None:
                wa_count = leetcode_ctx.wrong_answer_count
                if wa_count >= 4 and stress_integral > self.fatigue_load_threshold:
                    return LeetCodeMode.PANIC
            # Second: amygdala hijack
            if aai_score > self.aai_threshold:
                return LeetCodeMode.AMYGDALA_HIJACK
            # Third: destructive struggle
            if destructive.is_destructive:
                return LeetCodeMode.DESTRUCTIVE_STRUGGLE
            # Otherwise healthy challenge
            return LeetCodeMode.PRODUCTIVE_STRUGGLE

        if generic == "HYPO":
            # Fatigue outranks generic productive-struggle
            if stress_integral > self.fatigue_load_threshold:
                return LeetCodeMode.FATIGUE
            # Low stress hypo → calm but slow, still productive
            return LeetCodeMode.PRODUCTIVE_STRUGGLE

        if generic == "RECOVERY":
            if parasympathetic_rebound:
                # Special FLOW — learning window (flagged via rebound_flag)
                return LeetCodeMode.FLOW
            return LeetCodeMode.PRODUCTIVE_STRUGGLE

        # Defensive fallback for unexpected generic states
        logger.warning("unexpected generic state %r, defaulting to PRODUCTIVE_STRUGGLE", generic)
        return LeetCodeMode.PRODUCTIVE_STRUGGLE

    @staticmethod
    def _compute_confidence(
        state_confidence: float,
        destructive: DestructiveStruggleEstimate,
        mode: LeetCodeMode,
        aai_score: float,
    ) -> float:
        """Derive an overall confidence from the component confidences.

        For detector-driven modes the confidence is the geometric mean of the
        state engine confidence and the detector confidence, which keeps the
        estimate conservative when either source is uncertain.  For simple
        pass-through modes (FLOW, PRODUCTIVE_STRUGGLE) the state engine
        confidence is used directly.
        """
        if mode == LeetCodeMode.AMYGDALA_HIJACK:
            # Use aai_score itself as a proxy for detector confidence
            detector_conf = min(aai_score, 1.0)
            return (state_confidence * detector_conf) ** 0.5

        if mode == LeetCodeMode.DESTRUCTIVE_STRUGGLE:
            return (state_confidence * destructive.confidence) ** 0.5

        if mode == LeetCodeMode.FATIGUE:
            # No separate detector confidence; use stress as proxy
            return state_confidence * 0.85

        if mode == LeetCodeMode.PANIC:
            # High confidence when we trigger PANIC — both WA and load confirmed
            return state_confidence * 0.95

        # FLOW / PRODUCTIVE_STRUGGLE — pass through
        return state_confidence
