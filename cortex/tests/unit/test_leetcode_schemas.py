"""Tests for Cortex LeetCode domain schemas."""

from __future__ import annotations

import pytest

from cortex.libs.schemas.leetcode import (
    DestructiveStruggleEstimate,
    LeetCodeContext,
    LeetCodeMode,
    LeetCodeModeEstimate,
    LeetCodeSessionMetrics,
    LeetCodeSkillMetrics,
    LeetCodeStage,
    PatternLadderRung,
    SubmissionResult,
)


# ---------------------------------------------------------------------------
# LeetCodeStage and LeetCodeMode enums
# ---------------------------------------------------------------------------

class TestLeetCodeEnums:
    def test_stage_values(self):
        expected = {"READ", "PLAN", "IMPLEMENT", "DEBUG", "REFLECT"}
        actual = {s.value for s in LeetCodeStage}
        assert expected == actual

    def test_mode_values(self):
        expected = {
            "FLOW", "PRODUCTIVE_STRUGGLE", "DESTRUCTIVE_STRUGGLE",
            "PANIC", "FATIGUE", "AMYGDALA_HIJACK",
        }
        actual = {m.value for m in LeetCodeMode}
        assert expected == actual

    def test_submission_result_values(self):
        expected = {
            "Accepted", "Wrong Answer", "Runtime Error",
            "Time Limit Exceeded", "Memory Limit Exceeded", "Compile Error",
        }
        actual = {r.value for r in SubmissionResult}
        assert expected == actual


# ---------------------------------------------------------------------------
# LeetCodeContext
# ---------------------------------------------------------------------------

class TestLeetCodeContext:
    def test_defaults(self):
        ctx = LeetCodeContext()
        assert ctx.problem_id is None
        assert ctx.title == ""
        assert ctx.difficulty == ""
        assert ctx.tags == []
        assert ctx.time_elapsed_s == 0.0
        assert ctx.submission_count == 0
        assert ctx.wrong_answer_count == 0
        assert ctx.last_submission_result is None
        assert ctx.accepted is False
        assert ctx.stage == LeetCodeStage.READ
        assert ctx.code_snapshot == ""
        assert ctx.chars_per_min == 0.0
        assert ctx.solutions_tab_attempted is False

    def test_validation_rejects_negative_time(self):
        with pytest.raises(Exception):
            LeetCodeContext(time_elapsed_s=-1.0)


# ---------------------------------------------------------------------------
# DestructiveStruggleEstimate
# ---------------------------------------------------------------------------

class TestDestructiveStruggleEstimate:
    def test_defaults(self):
        est = DestructiveStruggleEstimate()
        assert est.is_destructive is False
        assert est.pathway == ""
        assert est.confidence == 0.0


# ---------------------------------------------------------------------------
# LeetCodeModeEstimate
# ---------------------------------------------------------------------------

class TestLeetCodeModeEstimate:
    def test_stage_mode_pair(self):
        est = LeetCodeModeEstimate(
            mode=LeetCodeMode.FLOW,
            stage=LeetCodeStage.IMPLEMENT,
        )
        assert est.stage_mode_pair == (LeetCodeStage.IMPLEMENT, LeetCodeMode.FLOW)

    def test_is_learning_window_true(self):
        est = LeetCodeModeEstimate(
            mode=LeetCodeMode.FLOW,
            stage=LeetCodeStage.REFLECT,
            parasympathetic_rebound=True,
        )
        assert est.is_learning_window is True

    def test_is_learning_window_false_wrong_stage(self):
        est = LeetCodeModeEstimate(
            mode=LeetCodeMode.FLOW,
            stage=LeetCodeStage.DEBUG,
            parasympathetic_rebound=True,
        )
        assert est.is_learning_window is False

    def test_is_learning_window_false_no_rebound(self):
        est = LeetCodeModeEstimate(
            mode=LeetCodeMode.FLOW,
            stage=LeetCodeStage.REFLECT,
            parasympathetic_rebound=False,
        )
        assert est.is_learning_window is False


# ---------------------------------------------------------------------------
# PatternLadderRung
# ---------------------------------------------------------------------------

class TestPatternLadderRung:
    def test_creation(self):
        rung = PatternLadderRung(level=2, label="Key Insight", content="Use a stack")
        assert rung.level == 2
        assert rung.label == "Key Insight"
        assert rung.content == "Use a stack"
        assert rung.revealed is False
        assert rung.revealed_at is None


# ---------------------------------------------------------------------------
# LeetCodeSessionMetrics
# ---------------------------------------------------------------------------

class TestLeetCodeSessionMetrics:
    def test_defaults(self):
        metrics = LeetCodeSessionMetrics(date="2026-03-13")
        assert metrics.problems_attempted == 0
        assert metrics.problems_accepted == 0
        assert metrics.total_time_s == 0.0
        assert metrics.avg_time_to_solve_s is None
        assert metrics.panic_episodes == 0
        assert metrics.lockout_count == 0
        assert metrics.pattern_ladder_max_depth == 0
        assert metrics.parasympathetic_windows == 0


# ---------------------------------------------------------------------------
# LeetCodeSkillMetrics — acceptance_rate property
# ---------------------------------------------------------------------------

class TestLeetCodeSkillMetrics:
    def test_acceptance_rate_zero_attempts(self):
        m = LeetCodeSkillMetrics(tag="DP")
        assert m.acceptance_rate == 0.0

    def test_acceptance_rate_with_attempts(self):
        m = LeetCodeSkillMetrics(tag="Graph", attempts=5, accepts=3)
        assert m.acceptance_rate == pytest.approx(0.6)
