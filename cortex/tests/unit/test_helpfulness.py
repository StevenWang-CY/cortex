"""Tests for the Helpfulness Tracker."""

from __future__ import annotations

import asyncio

import pytest

from cortex.services.eval.helpfulness import HelpfulnessTracker


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def tracker():
    return HelpfulnessTracker(store=None)


# ---------------------------------------------------------------------------
# Intervention -> FLOW in 60s + thumbs up -> reward ~0.5-1.0
# ---------------------------------------------------------------------------

class TestPositiveOutcome:
    def test_flow_recovery_with_thumbs_up(self, tracker):
        """
        Pre-state HYPER, post-state FLOW, thumbs_up rating.
        Reward should be in the 0.5-1.0 range.
        """
        iid = "int_positive"
        tracker.start_tracking(
            intervention_id=iid,
            intervention_type="overlay_only",
            state="HYPER",
            confidence=0.9,
            complexity=0.6,
            tab_count=15,
        )
        tracker.record_user_action(iid, "engaged")
        tracker.record_rating(iid, "thumbs_up")

        record = _run(tracker.end_tracking(
            intervention_id=iid,
            state="FLOW",
            confidence=0.95,
            complexity=0.2,
            tab_count=5,
        ))

        assert record is not None
        reward = record["reward_signal"]
        # Recovery(1.0)*0.4 + complexity_reduction*0.15 + rating(1.0)*0.3 + engaged(0.5)*0.15
        # = 0.4 + positive + 0.3 + 0.075 = ~0.775+
        assert 0.5 <= reward <= 1.0, f"Expected reward in [0.5, 1.0], got {reward}"

    def test_flow_recovery_without_rating(self, tracker):
        """Recovery to FLOW with engagement but no explicit rating."""
        iid = "int_no_rating"
        tracker.start_tracking(iid, "overlay_only", "HYPER", 0.9)
        tracker.record_user_action(iid, "engaged")

        record = _run(tracker.end_tracking(iid, "FLOW", 0.95))
        assert record is not None
        # Recovery (0.4) + engaged (0.075) = ~0.475, no rating contribution
        assert record["reward_signal"] > 0.0


# ---------------------------------------------------------------------------
# Dismissed + undo -> reward negative
# ---------------------------------------------------------------------------

class TestNegativeOutcome:
    def test_dismissed_and_undone(self, tracker):
        """Dismissed + undo should produce a negative reward."""
        iid = "int_negative"
        tracker.start_tracking(iid, "simplified_workspace", "HYPER", 0.8)
        tracker.record_undo(iid)

        record = _run(tracker.end_tracking(iid, "HYPER", 0.8))
        assert record is not None
        assert record["reward_signal"] < 0.0
        assert record["was_undone"] is True

    def test_ignored_intervention(self, tracker):
        """Intervention dismissed in <2s should register as ignored, reward negative."""
        iid = "int_ignored"
        tracker.start_tracking(iid, "overlay_only", "HYPO", 0.7)
        # Simulate fast dismissal (timestamp close to start)
        tracked = tracker._active[iid]
        tracker.record_user_action(iid, "dismissed", timestamp=tracked.started_at + 0.5)

        record = _run(tracker.end_tracking(iid, "HYPO", 0.6))
        assert record is not None
        assert record["was_ignored"] is True
        # HYPO post-state gives recovery = -0.3 * 0.4 = -0.12
        # ignored = -0.5 * 0.15 = -0.075
        assert record["reward_signal"] < 0.0


# ---------------------------------------------------------------------------
# start_tracking / end_tracking lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_start_creates_active_entry(self, tracker):
        tracker.start_tracking("id1", "breathing", "HYPER", 0.9)
        assert "id1" in tracker._active

    def test_end_removes_active_entry(self, tracker):
        tracker.start_tracking("id1", "breathing", "HYPER", 0.9)
        _run(tracker.end_tracking("id1", "FLOW", 0.95))
        assert "id1" not in tracker._active

    def test_end_without_start_returns_none(self, tracker):
        result = _run(tracker.end_tracking("nonexistent", "FLOW", 0.9))
        assert result is None

    def test_double_end_returns_none_second_time(self, tracker):
        tracker.start_tracking("id2", "overlay_only", "HYPER", 0.85)
        first = _run(tracker.end_tracking("id2", "FLOW", 0.9))
        second = _run(tracker.end_tracking("id2", "FLOW", 0.9))
        assert first is not None
        assert second is None

    def test_record_on_unknown_id_is_noop(self, tracker):
        # Should not raise
        tracker.record_user_action("unknown", "engaged")
        tracker.record_undo("unknown")
        tracker.record_rating("unknown", "thumbs_up")


# ---------------------------------------------------------------------------
# get_summary returns correct structure
# ---------------------------------------------------------------------------

class TestGetSummary:
    def test_empty_summary(self, tracker):
        summary = _run(tracker.get_summary())
        assert summary["total_tracked"] == 0
        assert summary["mean_reward"] == 0.0
        assert summary["positive_rate"] == 0.0

    def test_summary_after_tracking(self, tracker):
        # Track two interventions with different outcomes
        tracker.start_tracking("s1", "overlay_only", "HYPER", 0.9)
        tracker.record_rating("s1", "thumbs_up")
        _run(tracker.end_tracking("s1", "FLOW", 0.95))

        tracker.start_tracking("s2", "breathing", "HYPER", 0.8)
        tracker.record_undo("s2")
        _run(tracker.end_tracking("s2", "HYPER", 0.8))

        summary = _run(tracker.get_summary())
        assert summary["total_tracked"] == 2
        # At least one positive reward
        assert summary["positive_rate"] > 0.0

    def test_mean_reward_property(self, tracker):
        """mean_reward property should agree with get_summary."""
        tracker.start_tracking("m1", "overlay_only", "HYPER", 0.9)
        tracker.record_rating("m1", "thumbs_up")
        _run(tracker.end_tracking("m1", "FLOW", 0.95))

        summary = _run(tracker.get_summary())
        assert abs(tracker.mean_reward - summary["mean_reward"]) < 1e-9
