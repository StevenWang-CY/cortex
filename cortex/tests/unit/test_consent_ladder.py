"""Tests for the Consent Ladder and Policy."""

from __future__ import annotations

import asyncio

import pytest

from cortex.services.consent.ladder import (
    AUTONOMOUS_ACT,
    OBSERVE,
    PREVIEW,
    REVERSIBLE_ACT,
    SUGGEST,
    ConsentLadder,
)
from cortex.services.consent.policy import ConsentPolicy, DEFAULT_ACTION_LEVELS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def policy():
    return ConsentPolicy()


@pytest.fixture
def ladder(policy):
    return ConsentLadder(policy=policy, store=None)


# ---------------------------------------------------------------------------
# Fresh user cannot get autonomous_act
# ---------------------------------------------------------------------------

class TestFreshUserCannotGetAutonomousAct:
    def test_fresh_user_blocked_at_autonomous(self, ladder):
        """A brand-new user should not be allowed AUTONOMOUS_ACT for any action."""
        decision = _run(ladder.check("close_tab", requested_level=AUTONOMOUS_ACT))
        assert not decision.allowed or decision.effective_level < AUTONOMOUS_ACT

    def test_fresh_user_default_level(self, ladder):
        """Fresh ladder starts at the policy minimum for the action type."""
        level = _run(ladder.get_level("close_tab"))
        # close_tab minimum is PREVIEW (2)
        assert level == PREVIEW

    def test_fresh_user_suggest_action_allowed(self, ladder):
        """SUGGEST-level actions should be allowed for fresh users."""
        decision = _run(ladder.check("show_overlay", requested_level=SUGGEST))
        assert decision.allowed


# ---------------------------------------------------------------------------
# 5 approvals escalates consent level
# ---------------------------------------------------------------------------

class TestEscalation:
    def test_five_approvals_escalates(self, ladder):
        """After 5 approvals the consent level should increase by one."""
        action = "close_tab"
        initial_level = _run(ladder.get_level(action))

        for _ in range(5):
            _run(ladder.record_approval(action))

        new_level = _run(ladder.get_level(action))
        assert new_level == initial_level + 1

    def test_escalation_resets_approval_counter(self, ladder):
        """After escalation, approval counter resets so another 5 are needed."""
        action = "close_tab"
        for _ in range(5):
            _run(ladder.record_approval(action))

        # Level should have escalated once
        level_after_first = _run(ladder.get_level(action))

        # 4 more approvals should NOT escalate again
        for _ in range(4):
            _run(ladder.record_approval(action))
        assert _run(ladder.get_level(action)) == level_after_first

        # 1 more (total 5 since last escalation) should escalate
        _run(ladder.record_approval(action))
        assert _run(ladder.get_level(action)) == level_after_first + 1

    def test_cannot_escalate_past_autonomous(self, ladder):
        """Level should cap at AUTONOMOUS_ACT (4)."""
        action = "show_overlay"  # starts at SUGGEST (1)
        # Need 5 approvals * 3 escalations to go from 1 -> 4
        for _ in range(5 * 4):
            _run(ladder.record_approval(action))
        assert _run(ladder.get_level(action)) == AUTONOMOUS_ACT

        # One more batch should not exceed 4
        for _ in range(5):
            _run(ladder.record_approval(action))
        assert _run(ladder.get_level(action)) == AUTONOMOUS_ACT


# ---------------------------------------------------------------------------
# 3 rejections de-escalates
# ---------------------------------------------------------------------------

class TestDeescalation:
    def test_three_rejections_deescalates(self, ladder):
        """3 rejections should lower the consent level by one."""
        action = "close_tab"
        # Escalate first so there is room to de-escalate
        for _ in range(5):
            _run(ladder.record_approval(action))
        level_before = _run(ladder.get_level(action))
        assert level_before > SUGGEST

        for _ in range(3):
            _run(ladder.record_rejection(action))

        level_after = _run(ladder.get_level(action))
        assert level_after == level_before - 1

    def test_cannot_deescalate_below_suggest(self, ladder):
        """De-escalation should not go below SUGGEST (1)."""
        action = "show_overlay"  # starts at SUGGEST
        for _ in range(10):
            _run(ladder.record_rejection(action))
        assert _run(ladder.get_level(action)) >= SUGGEST


# ---------------------------------------------------------------------------
# Policy maps action types to minimum levels correctly
# ---------------------------------------------------------------------------

class TestPolicyMapping:
    def test_suggest_level_actions(self, policy):
        for action in ("show_overlay", "highlight_tab", "start_timer"):
            assert policy.get_minimum_level(action) == SUGGEST

    def test_preview_level_actions(self, policy):
        for action in ("close_tab", "group_tabs", "bookmark_and_close"):
            assert policy.get_minimum_level(action) == PREVIEW

    def test_reversible_act_actions(self, policy):
        for action in ("open_url", "disable_copilot"):
            assert policy.get_minimum_level(action) == REVERSIBLE_ACT

    def test_autonomous_act_actions(self, policy):
        for action in ("shutdown_workspace", "launch_project"):
            assert policy.get_minimum_level(action) == AUTONOMOUS_ACT

    def test_unknown_action_defaults_to_preview(self, policy):
        assert policy.get_minimum_level("totally_unknown_action") == PREVIEW

    def test_global_max_default(self, policy):
        assert policy.global_max_level == REVERSIBLE_ACT


# ---------------------------------------------------------------------------
# Check returns allowed/blocked correctly
# ---------------------------------------------------------------------------

class TestCheckDecision:
    def test_allowed_when_level_sufficient(self, ladder):
        """Request at or below earned level should be allowed."""
        decision = _run(ladder.check("show_overlay", requested_level=SUGGEST))
        assert decision.allowed
        assert decision.effective_level == SUGGEST

    def test_blocked_when_level_insufficient(self, ladder):
        """Request above earned level should be downgraded."""
        decision = _run(ladder.check("close_tab", requested_level=AUTONOMOUS_ACT))
        # effective_level should be capped
        assert decision.effective_level < AUTONOMOUS_ACT

    def test_global_max_caps_level(self):
        """Global max should cap what is allowed regardless of earned level."""
        policy = ConsentPolicy(global_max_level=SUGGEST)
        ladder = ConsentLadder(policy=policy, store=None)

        # Even after many approvals, global cap prevents escalation beyond SUGGEST
        for _ in range(20):
            _run(ladder.record_approval("show_overlay"))

        decision = _run(ladder.check("show_overlay", requested_level=PREVIEW))
        assert decision.effective_level <= SUGGEST
