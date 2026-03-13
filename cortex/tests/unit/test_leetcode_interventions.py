"""
Tests for LeetCode Interventions — Stage x Mode Matrix

Covers:
- RestatementScratchpad triggers at (READ, DESTRUCTIVE_STRUGGLE) with comprehension pathway
- PatternLadder triggers at (PLAN, PRODUCTIVE_STRUGGLE)
- AmygdalaLockout triggers at (DEBUG, AMYGDALA_HIJACK)
- SubmissionDisciplineGuard triggers when wrong_answer_count > 2
- SolutionEscapeFriction triggers at PANIC + solutions_tab_attempted
- InterventionMatrix.select returns correct actions
- Cooldown enforcement
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from cortex.libs.schemas.leetcode import (
    DestructiveStruggleEstimate,
    LeetCodeContext,
    LeetCodeMode,
    LeetCodeModeEstimate,
    LeetCodeStage,
)
from cortex.services.intervention_engine.leetcode_interventions import (
    AmygdalaLockout,
    InterventionMatrix,
    PatternLadder,
    RestatementScratchpad,
    SolutionEscapeFriction,
    SubmissionDisciplineGuard,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mode_estimate(
    stage: LeetCodeStage = LeetCodeStage.DEBUG,
    mode: LeetCodeMode = LeetCodeMode.PRODUCTIVE_STRUGGLE,
    pathway: str = "",
    is_destructive: bool = False,
) -> LeetCodeModeEstimate:
    return LeetCodeModeEstimate(
        mode=mode,
        stage=stage,
        confidence=0.9,
        aai_score=0.5,
        allostatic_load=100.0,
        destructive=DestructiveStruggleEstimate(
            is_destructive=is_destructive,
            pathway=pathway,
            confidence=0.8,
        ),
    )


def _make_ctx(
    stage: LeetCodeStage = LeetCodeStage.DEBUG,
    wrong_answer_count: int = 0,
    solutions_tab_attempted: bool = False,
) -> LeetCodeContext:
    return LeetCodeContext(
        problem_id="42",
        title="Two Sum",
        difficulty="Easy",
        tags=["Array", "Hash Table"],
        stage=stage,
        wrong_answer_count=wrong_answer_count,
        solutions_tab_attempted=solutions_tab_attempted,
        time_elapsed_s=300.0,
    )


# ---------------------------------------------------------------------------
# RestatementScratchpad
# ---------------------------------------------------------------------------


class TestRestatementScratchpad:

    def test_triggers_at_read_destructive_comprehension(self):
        intervention = RestatementScratchpad()
        mode_est = _make_mode_estimate(
            stage=LeetCodeStage.READ,
            mode=LeetCodeMode.DESTRUCTIVE_STRUGGLE,
            pathway="comprehension",
            is_destructive=True,
        )
        ctx = _make_ctx(stage=LeetCodeStage.READ)
        assert intervention.should_trigger(mode_est, ctx) is True

    def test_does_not_trigger_without_comprehension_pathway(self):
        intervention = RestatementScratchpad()
        mode_est = _make_mode_estimate(
            stage=LeetCodeStage.READ,
            mode=LeetCodeMode.DESTRUCTIVE_STRUGGLE,
            pathway="implementation",
            is_destructive=True,
        )
        ctx = _make_ctx(stage=LeetCodeStage.READ)
        assert intervention.should_trigger(mode_est, ctx) is False

    def test_does_not_trigger_at_wrong_stage(self):
        intervention = RestatementScratchpad()
        mode_est = _make_mode_estimate(
            stage=LeetCodeStage.IMPLEMENT,
            mode=LeetCodeMode.DESTRUCTIVE_STRUGGLE,
            pathway="comprehension",
            is_destructive=True,
        )
        ctx = _make_ctx(stage=LeetCodeStage.IMPLEMENT)
        assert intervention.should_trigger(mode_est, ctx) is False


# ---------------------------------------------------------------------------
# PatternLadder
# ---------------------------------------------------------------------------


class TestPatternLadder:

    def test_triggers_at_plan_productive_struggle(self):
        intervention = PatternLadder()
        mode_est = _make_mode_estimate(
            stage=LeetCodeStage.PLAN,
            mode=LeetCodeMode.PRODUCTIVE_STRUGGLE,
        )
        ctx = _make_ctx(stage=LeetCodeStage.PLAN)
        assert intervention.should_trigger(mode_est, ctx) is True

    def test_does_not_trigger_at_wrong_mode(self):
        intervention = PatternLadder()
        mode_est = _make_mode_estimate(
            stage=LeetCodeStage.PLAN,
            mode=LeetCodeMode.FLOW,
        )
        ctx = _make_ctx(stage=LeetCodeStage.PLAN)
        assert intervention.should_trigger(mode_est, ctx) is False


# ---------------------------------------------------------------------------
# AmygdalaLockout
# ---------------------------------------------------------------------------


class TestAmygdalaLockout:

    def test_triggers_at_debug_amygdala_hijack(self):
        intervention = AmygdalaLockout()
        mode_est = _make_mode_estimate(
            stage=LeetCodeStage.DEBUG,
            mode=LeetCodeMode.AMYGDALA_HIJACK,
        )
        ctx = _make_ctx(stage=LeetCodeStage.DEBUG)
        assert intervention.should_trigger(mode_est, ctx) is True

    def test_does_not_trigger_at_wrong_stage(self):
        intervention = AmygdalaLockout()
        mode_est = _make_mode_estimate(
            stage=LeetCodeStage.PLAN,
            mode=LeetCodeMode.AMYGDALA_HIJACK,
        )
        ctx = _make_ctx(stage=LeetCodeStage.PLAN)
        assert intervention.should_trigger(mode_est, ctx) is False

    def test_debounce_prevents_retrigger(self):
        """Lockout should not re-fire within 100s debounce window."""
        intervention = AmygdalaLockout()
        mode_est = _make_mode_estimate(
            stage=LeetCodeStage.DEBUG,
            mode=LeetCodeMode.AMYGDALA_HIJACK,
        )
        ctx = _make_ctx(stage=LeetCodeStage.DEBUG)
        assert intervention.should_trigger(mode_est, ctx) is True
        intervention.build_action(mode_est, ctx)
        # Immediately after: should be blocked by debounce
        assert intervention.should_trigger(mode_est, ctx) is False

    def test_escalation_base_duration(self):
        """Low WA count → base 90s duration."""
        intervention = AmygdalaLockout()
        mode_est = _make_mode_estimate(
            stage=LeetCodeStage.DEBUG,
            mode=LeetCodeMode.AMYGDALA_HIJACK,
        )
        ctx = _make_ctx(stage=LeetCodeStage.DEBUG, wrong_answer_count=2)
        action = intervention.build_action(mode_est, ctx)
        assert action["payload"]["duration_s"] == 90

    def test_escalation_high_wa_increases_duration(self):
        """High WA count escalates duration beyond base."""
        intervention = AmygdalaLockout()
        mode_est = _make_mode_estimate(
            stage=LeetCodeStage.DEBUG,
            mode=LeetCodeMode.AMYGDALA_HIJACK,
        )
        ctx = _make_ctx(stage=LeetCodeStage.DEBUG, wrong_answer_count=6)
        action = intervention.build_action(mode_est, ctx)
        # 6 WA: 90 + (6-3)*30 = 180
        assert action["payload"]["duration_s"] == 180

    def test_escalation_caps_at_max(self):
        """Duration is capped at 180s."""
        intervention = AmygdalaLockout()
        mode_est = _make_mode_estimate(
            stage=LeetCodeStage.DEBUG,
            mode=LeetCodeMode.AMYGDALA_HIJACK,
        )
        ctx = _make_ctx(stage=LeetCodeStage.DEBUG, wrong_answer_count=20)
        action = intervention.build_action(mode_est, ctx)
        assert action["payload"]["duration_s"] == 180


# ---------------------------------------------------------------------------
# SubmissionDisciplineGuard
# ---------------------------------------------------------------------------


class TestSubmissionDisciplineGuard:

    def test_triggers_when_wrong_answers_exceed_threshold(self):
        intervention = SubmissionDisciplineGuard()
        mode_est = _make_mode_estimate(
            stage=LeetCodeStage.DEBUG,
            mode=LeetCodeMode.PRODUCTIVE_STRUGGLE,
        )
        ctx = _make_ctx(stage=LeetCodeStage.DEBUG, wrong_answer_count=3)
        assert intervention.should_trigger(mode_est, ctx) is True

    def test_does_not_trigger_at_threshold(self):
        intervention = SubmissionDisciplineGuard()
        mode_est = _make_mode_estimate(
            stage=LeetCodeStage.DEBUG,
            mode=LeetCodeMode.PRODUCTIVE_STRUGGLE,
        )
        ctx = _make_ctx(stage=LeetCodeStage.DEBUG, wrong_answer_count=2)
        assert intervention.should_trigger(mode_est, ctx) is False


# ---------------------------------------------------------------------------
# SolutionEscapeFriction
# ---------------------------------------------------------------------------


class TestSolutionEscapeFriction:

    def test_triggers_at_panic_with_solutions_tab(self):
        intervention = SolutionEscapeFriction()
        mode_est = _make_mode_estimate(
            stage=LeetCodeStage.IMPLEMENT,
            mode=LeetCodeMode.PANIC,
        )
        ctx = _make_ctx(
            stage=LeetCodeStage.IMPLEMENT,
            solutions_tab_attempted=True,
        )
        assert intervention.should_trigger(mode_est, ctx) is True

    def test_does_not_trigger_without_solutions_tab(self):
        intervention = SolutionEscapeFriction()
        mode_est = _make_mode_estimate(
            stage=LeetCodeStage.IMPLEMENT,
            mode=LeetCodeMode.PANIC,
        )
        ctx = _make_ctx(
            stage=LeetCodeStage.IMPLEMENT,
            solutions_tab_attempted=False,
        )
        assert intervention.should_trigger(mode_est, ctx) is False


# ---------------------------------------------------------------------------
# InterventionMatrix
# ---------------------------------------------------------------------------


class TestInterventionMatrix:

    def test_select_returns_matching_actions(self):
        matrix = InterventionMatrix()
        mode_est = _make_mode_estimate(
            stage=LeetCodeStage.DEBUG,
            mode=LeetCodeMode.AMYGDALA_HIJACK,
        )
        ctx = _make_ctx(stage=LeetCodeStage.DEBUG)
        actions = matrix.select(mode_est, ctx)
        action_types = [a["action"] for a in actions]
        assert "show_lockout" in action_types


# ---------------------------------------------------------------------------
# Cooldown enforcement
# ---------------------------------------------------------------------------


class TestCooldownEnforcement:

    def test_cooldown_prevents_immediate_retrigger(self):
        intervention = RestatementScratchpad()
        mode_est = _make_mode_estimate(
            stage=LeetCodeStage.READ,
            mode=LeetCodeMode.DESTRUCTIVE_STRUGGLE,
            pathway="comprehension",
            is_destructive=True,
        )
        ctx = _make_ctx(stage=LeetCodeStage.READ)

        # First trigger fires
        assert intervention.should_trigger(mode_est, ctx) is True
        intervention.build_action(mode_est, ctx)  # records trigger time

        # Immediate re-trigger should be blocked by cooldown
        assert intervention.should_trigger(mode_est, ctx) is False
