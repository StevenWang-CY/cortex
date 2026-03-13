"""
Tests for LeetCodeLongitudinalTracker

Covers:
- record_problem_attempt increments count
- record_problem_accepted updates metrics and skill tracking
- should_end_session returns True when load > budget
- budget_remaining and budget_ratio properties
- reset_session clears session metrics but keeps skills
- to_dict / from_dict round-trip
"""

from __future__ import annotations

import pytest

from cortex.libs.schemas.leetcode import LeetCodeContext, LeetCodeStage
from cortex.services.state_engine.leetcode_longitudinal import LeetCodeLongitudinalTracker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(
    tags: list[str] | None = None,
    stage: LeetCodeStage = LeetCodeStage.DEBUG,
) -> LeetCodeContext:
    if tags is None:
        tags = ["Array", "Hash Table"]
    return LeetCodeContext(
        problem_id="1",
        title="Two Sum",
        difficulty="Easy",
        tags=tags,
        stage=stage,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRecordProblemAttempt:

    def test_increments_problems_attempted(self):
        tracker = LeetCodeLongitudinalTracker(daily_load_budget=600.0)
        ctx = _make_ctx()
        tracker.record_problem_attempt(ctx)
        assert tracker.session_metrics.problems_attempted == 1

    def test_increments_per_tag_attempts(self):
        tracker = LeetCodeLongitudinalTracker()
        ctx = _make_ctx(tags=["DP", "Graph"])
        tracker.record_problem_attempt(ctx)
        skills = tracker.get_skill_metrics()
        assert skills["DP"].attempts == 1
        assert skills["Graph"].attempts == 1

    def test_multiple_attempts_accumulate(self):
        tracker = LeetCodeLongitudinalTracker()
        ctx = _make_ctx()
        tracker.record_problem_attempt(ctx)
        tracker.record_problem_attempt(ctx)
        assert tracker.session_metrics.problems_attempted == 2


class TestRecordProblemAccepted:

    def test_updates_accepted_count(self):
        tracker = LeetCodeLongitudinalTracker()
        ctx = _make_ctx()
        tracker.record_problem_accepted(ctx, time_to_solve_s=180.0)
        assert tracker.session_metrics.problems_accepted == 1

    def test_updates_avg_solve_time(self):
        tracker = LeetCodeLongitudinalTracker()
        ctx = _make_ctx()
        tracker.record_problem_accepted(ctx, time_to_solve_s=100.0)
        tracker.record_problem_accepted(ctx, time_to_solve_s=200.0)
        assert tracker.session_metrics.avg_time_to_solve_s == pytest.approx(150.0)

    def test_updates_per_tag_accepts(self):
        tracker = LeetCodeLongitudinalTracker()
        ctx = _make_ctx(tags=["Array"])
        tracker.record_problem_accepted(ctx, time_to_solve_s=120.0)
        skills = tracker.get_skill_metrics()
        assert skills["Array"].accepts == 1


class TestShouldEndSession:

    def test_returns_true_when_load_exceeds_budget(self):
        tracker = LeetCodeLongitudinalTracker(daily_load_budget=500.0)
        tracker.update_load(600.0)
        assert tracker.should_end_session() is True

    def test_returns_false_when_load_within_budget(self):
        tracker = LeetCodeLongitudinalTracker(daily_load_budget=500.0)
        tracker.update_load(300.0)
        assert tracker.should_end_session() is False


class TestBudgetProperties:

    def test_budget_remaining(self):
        tracker = LeetCodeLongitudinalTracker(daily_load_budget=600.0)
        tracker.update_load(200.0)
        assert tracker.budget_remaining == pytest.approx(400.0)

    def test_budget_ratio(self):
        tracker = LeetCodeLongitudinalTracker(daily_load_budget=600.0)
        tracker.update_load(300.0)
        assert tracker.budget_ratio == pytest.approx(0.5)

    def test_budget_ratio_zero_budget(self):
        tracker = LeetCodeLongitudinalTracker(daily_load_budget=0.0)
        assert tracker.budget_ratio == 1.0


class TestResetSession:

    def test_clears_session_metrics(self):
        tracker = LeetCodeLongitudinalTracker()
        ctx = _make_ctx()
        tracker.record_problem_attempt(ctx)
        tracker.update_load(300.0)
        tracker.reset_session("2026-03-14")
        assert tracker.session_metrics.problems_attempted == 0
        assert tracker.session_metrics.date == "2026-03-14"

    def test_preserves_skill_metrics(self):
        tracker = LeetCodeLongitudinalTracker()
        ctx = _make_ctx(tags=["DP"])
        tracker.record_problem_attempt(ctx)
        tracker.reset_session("2026-03-14")
        skills = tracker.get_skill_metrics()
        assert "DP" in skills
        assert skills["DP"].attempts == 1


class TestSerialisationRoundTrip:

    def test_to_dict_from_dict_preserves_state(self):
        tracker = LeetCodeLongitudinalTracker(daily_load_budget=800.0)
        ctx = _make_ctx(tags=["Graph", "BFS"])
        tracker.record_problem_attempt(ctx)
        tracker.record_problem_accepted(ctx, time_to_solve_s=240.0)
        tracker.update_load(350.0)

        data = tracker.to_dict()
        restored = LeetCodeLongitudinalTracker.from_dict(data)

        assert restored.session_metrics.problems_attempted == 1
        assert restored.session_metrics.problems_accepted == 1
        assert restored.session_metrics.avg_time_to_solve_s == pytest.approx(240.0)
        assert restored.budget_remaining == pytest.approx(450.0)
        skills = restored.get_skill_metrics()
        assert skills["Graph"].accepts == 1
        assert skills["BFS"].accepts == 1
