"""
Tests for LeetCodeModeResolver

Covers mode resolution from generic StateEstimate + biological detector outputs:
- FLOW pass-through
- HYPER + AAI → AMYGDALA_HIJACK
- HYPER + destructive struggle → DESTRUCTIVE_STRUGGLE
- HYPER + non-destructive → PRODUCTIVE_STRUGGLE
- HYPO + high allostatic load → FATIGUE
- HYPO + low stress → PRODUCTIVE_STRUGGLE
- RECOVERY + parasympathetic rebound → FLOW with rebound flag
- RECOVERY without rebound → PRODUCTIVE_STRUGGLE
"""

from __future__ import annotations

from cortex.libs.schemas.leetcode import (
    DestructiveStruggleEstimate,
    LeetCodeContext,
    LeetCodeMode,
    LeetCodeStage,
)
from cortex.libs.schemas.state import SignalQuality, StateEstimate, StateScores
from cortex.services.state_engine.leetcode_mode_resolver import LeetCodeModeResolver

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_estimate(
    state: str = "HYPER",
    confidence: float = 0.9,
    stress_integral: float | None = None,
) -> StateEstimate:
    return StateEstimate(
        state=state,
        confidence=confidence,
        scores=StateScores(flow=0.1, hypo=0.1, hyper=0.8, recovery=0.0),
        signal_quality=SignalQuality(physio=0.8, kinematics=0.7, telemetry=0.9),
        timestamp=1000.0,
        stress_integral=stress_integral,
    )


def _make_destructive(
    is_destructive: bool = False,
    pathway: str = "",
    confidence: float = 0.8,
) -> DestructiveStruggleEstimate:
    return DestructiveStruggleEstimate(
        is_destructive=is_destructive,
        pathway=pathway,
        confidence=confidence,
    )


def _default_ctx() -> LeetCodeContext:
    return LeetCodeContext(stage=LeetCodeStage.DEBUG)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLeetCodeModeResolver:
    """Test mode resolution rules."""

    def setup_method(self):
        self.resolver = LeetCodeModeResolver(
            aai_threshold=0.7,
            fatigue_load_threshold=400.0,
        )
        self.ctx = _default_ctx()

    def test_flow_state_maps_to_flow_mode(self):
        estimate = _make_estimate(state="FLOW")
        result = self.resolver.resolve(
            state_estimate=estimate,
            leetcode_ctx=self.ctx,
            aai_score=0.0,
            destructive=_make_destructive(),
            parasympathetic_rebound=False,
        )
        assert result.mode == LeetCodeMode.FLOW

    def test_hyper_high_aai_maps_to_amygdala_hijack(self):
        estimate = _make_estimate(state="HYPER")
        result = self.resolver.resolve(
            state_estimate=estimate,
            leetcode_ctx=self.ctx,
            aai_score=0.85,
            destructive=_make_destructive(),
            parasympathetic_rebound=False,
        )
        assert result.mode == LeetCodeMode.AMYGDALA_HIJACK

    def test_hyper_destructive_struggle(self):
        estimate = _make_estimate(state="HYPER")
        result = self.resolver.resolve(
            state_estimate=estimate,
            leetcode_ctx=self.ctx,
            aai_score=0.3,
            destructive=_make_destructive(is_destructive=True, pathway="comprehension"),
            parasympathetic_rebound=False,
        )
        assert result.mode == LeetCodeMode.DESTRUCTIVE_STRUGGLE

    def test_hyper_not_destructive_maps_to_productive_struggle(self):
        estimate = _make_estimate(state="HYPER")
        result = self.resolver.resolve(
            state_estimate=estimate,
            leetcode_ctx=self.ctx,
            aai_score=0.3,
            destructive=_make_destructive(is_destructive=False),
            parasympathetic_rebound=False,
        )
        assert result.mode == LeetCodeMode.PRODUCTIVE_STRUGGLE

    def test_hypo_high_allostatic_load_maps_to_fatigue(self):
        estimate = _make_estimate(state="HYPO", stress_integral=500.0)
        result = self.resolver.resolve(
            state_estimate=estimate,
            leetcode_ctx=self.ctx,
            aai_score=0.1,
            destructive=_make_destructive(),
            parasympathetic_rebound=False,
        )
        assert result.mode == LeetCodeMode.FATIGUE

    def test_hypo_low_stress_maps_to_productive_struggle(self):
        estimate = _make_estimate(state="HYPO", stress_integral=100.0)
        result = self.resolver.resolve(
            state_estimate=estimate,
            leetcode_ctx=self.ctx,
            aai_score=0.1,
            destructive=_make_destructive(),
            parasympathetic_rebound=False,
        )
        assert result.mode == LeetCodeMode.PRODUCTIVE_STRUGGLE

    def test_recovery_with_parasympathetic_rebound_maps_to_flow(self):
        estimate = _make_estimate(state="RECOVERY")
        result = self.resolver.resolve(
            state_estimate=estimate,
            leetcode_ctx=self.ctx,
            aai_score=0.1,
            destructive=_make_destructive(),
            parasympathetic_rebound=True,
        )
        assert result.mode == LeetCodeMode.FLOW
        assert result.parasympathetic_rebound is True

    def test_recovery_without_rebound_maps_to_productive_struggle(self):
        estimate = _make_estimate(state="RECOVERY")
        result = self.resolver.resolve(
            state_estimate=estimate,
            leetcode_ctx=self.ctx,
            aai_score=0.1,
            destructive=_make_destructive(),
            parasympathetic_rebound=False,
        )
        assert result.mode == LeetCodeMode.PRODUCTIVE_STRUGGLE
        assert result.parasympathetic_rebound is False

    def test_hyper_rapid_wa_plus_high_load_maps_to_panic(self):
        """PANIC = HYPER + many WAs + high allostatic load."""
        ctx = LeetCodeContext(stage=LeetCodeStage.DEBUG, wrong_answer_count=5)
        estimate = _make_estimate(state="HYPER", stress_integral=500.0)
        result = self.resolver.resolve(
            state_estimate=estimate,
            leetcode_ctx=ctx,
            aai_score=0.3,
            destructive=_make_destructive(),
            parasympathetic_rebound=False,
        )
        assert result.mode == LeetCodeMode.PANIC

    def test_panic_takes_priority_over_amygdala_hijack(self):
        """PANIC outranks AMYGDALA_HIJACK when both conditions met."""
        ctx = LeetCodeContext(stage=LeetCodeStage.DEBUG, wrong_answer_count=4)
        estimate = _make_estimate(state="HYPER", stress_integral=500.0)
        result = self.resolver.resolve(
            state_estimate=estimate,
            leetcode_ctx=ctx,
            aai_score=0.9,  # high AAI
            destructive=_make_destructive(),
            parasympathetic_rebound=False,
        )
        assert result.mode == LeetCodeMode.PANIC

    def test_hyper_high_wa_but_low_load_maps_to_amygdala_not_panic(self):
        """High WA count but low stress integral → AAI takes over, not PANIC."""
        ctx = LeetCodeContext(stage=LeetCodeStage.DEBUG, wrong_answer_count=5)
        estimate = _make_estimate(state="HYPER", stress_integral=100.0)
        result = self.resolver.resolve(
            state_estimate=estimate,
            leetcode_ctx=ctx,
            aai_score=0.9,
            destructive=_make_destructive(),
            parasympathetic_rebound=False,
        )
        assert result.mode == LeetCodeMode.AMYGDALA_HIJACK
