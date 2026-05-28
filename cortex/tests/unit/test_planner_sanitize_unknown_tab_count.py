"""P1-6: sanitize_plan_actions must drop actions with tab_index when tab_count is None.

When tab_count is None (browser context not available), any action that
specifies a concrete tab_index cannot be validated and is silently
dangerous — it could close or modify an arbitrary tab. The fix drops
those actions and emits a warning.
"""

from __future__ import annotations

import logging

from cortex.libs.schemas.intervention import (
    InterventionPlan,
    MicroStep,
    SuggestedAction,
    UIPlan,
)
from cortex.services.intervention_engine.planner import sanitize_plan_actions


def _make_plan(actions: list[SuggestedAction]) -> InterventionPlan:
    return InterventionPlan(
        level="simplified_workspace",
        situation_summary="Test plan for sanitizer.",
        headline="Focus on one thing",
        primary_focus="The current file",
        micro_steps=[MicroStep(text="Close extra tabs")],
        ui_plan=UIPlan(),
        suggested_actions=actions,
    )


def _make_tab_action(tab_index: int) -> SuggestedAction:
    return SuggestedAction(
        action_type="close_tab",
        tab_index=tab_index,
        label="Close distracting tab",
        reversible=True,
    )


def _make_safe_action() -> SuggestedAction:
    return SuggestedAction(
        action_type="suggest_movement_break",
        label="Take a quick stretch",
        reversible=True,
    )


class TestSanitizePlanActionsUnknownTabCount:
    def test_tab_index_action_dropped_when_tab_count_none(self, caplog) -> None:
        """An action with tab_index=99 must be dropped when tab_count=None."""
        action = _make_tab_action(tab_index=99)
        plan = _make_plan([action])

        with caplog.at_level(logging.WARNING, logger="cortex.services.intervention_engine.planner"):
            warnings = sanitize_plan_actions(plan, tab_count=None)

        assert plan.suggested_actions == [], (
            "tab_index action should have been removed from plan"
        )
        assert len(warnings) == 1
        assert "tab_count is unknown" in warnings[0]
        # Check the warning surfaced in the logger
        assert any("tab_count is unknown" in r.message for r in caplog.records), (
            f"expected logger.warning about unknown tab_count; got: {caplog.records}"
        )

    def test_non_tab_index_action_retained(self) -> None:
        """Actions without tab_index are kept even when tab_count=None."""
        safe = _make_safe_action()
        plan = _make_plan([safe])

        warnings = sanitize_plan_actions(plan, tab_count=None)

        assert len(plan.suggested_actions) == 1
        assert plan.suggested_actions[0].action_type == "suggest_movement_break"
        assert len(warnings) == 0

    def test_mixed_actions_tab_index_dropped_safe_kept(self) -> None:
        """Only actions with non-None tab_index are dropped; others pass through."""
        tab_action = _make_tab_action(tab_index=3)
        safe_action = _make_safe_action()
        plan = _make_plan([tab_action, safe_action])

        warnings = sanitize_plan_actions(plan, tab_count=None)

        assert len(plan.suggested_actions) == 1
        assert plan.suggested_actions[0] is safe_action
        assert len(warnings) == 1

    def test_tab_index_valid_when_tab_count_known(self) -> None:
        """When tab_count is supplied, in-range tab actions are kept (existing behavior)."""
        action = _make_tab_action(tab_index=2)
        plan = _make_plan([action])

        warnings = sanitize_plan_actions(plan, tab_count=5)

        assert len(plan.suggested_actions) == 1
        assert len(warnings) == 0

    def test_tab_index_out_of_range_dropped_when_tab_count_known(self) -> None:
        """Out-of-range tab_index still dropped when tab_count is known (existing behavior)."""
        action = _make_tab_action(tab_index=99)
        plan = _make_plan([action])

        warnings = sanitize_plan_actions(plan, tab_count=5)

        assert len(plan.suggested_actions) == 0
        assert len(warnings) == 1
