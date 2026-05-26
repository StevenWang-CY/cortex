"""P0 §3.7: promote_biology_break planner unit tests."""

from __future__ import annotations

from cortex.libs.schemas.intervention import (
    InterventionPlan,
    MicroStep,
    SuggestedAction,
    UIPlan,
)
from cortex.services.intervention_engine.planner import promote_biology_break


def _plan_with_actions() -> InterventionPlan:
    return InterventionPlan(
        intervention_id="int_test_001",
        level="simplified_workspace",
        situation_summary="Stress is high; refocusing the workspace.",
        headline="Refocus on the auth bug",
        primary_focus="auth.py",
        micro_steps=[MicroStep(text="Re-read the failing test", status="pending")],
        ui_plan=UIPlan(
            dim_background=True,
            show_overlay=True,
            fold_unrelated_code=True,
            intervention_type="simplified_workspace",
        ),
        tone="direct",
        suggested_actions=[
            SuggestedAction(
                action_type="close_tab",
                tab_index=3,
                label="Close ad hoc tab",
                category="recommended",
            ),
            SuggestedAction(
                action_type="search_error",
                target="ImportError urllib",
                label="Search for the import error",
                category="recommended",
            ),
        ],
    )


def test_promote_biology_break_prepends_action() -> None:
    plan = _plan_with_actions()
    out = promote_biology_break(
        plan, duration_seconds=240, breathing_pattern="box",
    )
    assert out is plan  # mutated in place
    assert plan.suggested_actions
    first = plan.suggested_actions[0]
    assert first.action_type == "take_biology_break"
    assert first.metadata["duration_seconds"] == 240
    assert first.metadata["breathing_pattern"] == "box"


def test_promote_biology_break_downgrades_peer_actions() -> None:
    plan = _plan_with_actions()
    promote_biology_break(plan)
    peers = plan.suggested_actions[1:]
    assert peers, "expected the prior actions to be preserved as optional"
    for action in peers:
        assert action.category == "optional"


def test_promote_biology_break_forces_overlay_only() -> None:
    plan = _plan_with_actions()
    promote_biology_break(plan)
    assert plan.ui_plan.intervention_type == "overlay_only"
    assert plan.level == "overlay_only"
    assert "break" in plan.headline.lower()
    assert plan.primary_focus == "Your breath"


def test_promote_biology_break_metadata_defaults() -> None:
    plan = _plan_with_actions()
    promote_biology_break(plan, breathing_pattern=None)
    first = plan.suggested_actions[0]
    # When no explicit pattern is supplied the daemon stamps ``auto`` so
    # the BiologyBreakController can decide from live HRV.
    assert first.metadata["breathing_pattern"] == "auto"
    assert first.metadata["audio_cue"] is True


def test_promote_biology_break_idempotent_under_duplicate_call() -> None:
    """Promoting twice prepends a second break — caller's responsibility,
    but the schema must accept it."""
    plan = _plan_with_actions()
    promote_biology_break(plan)
    promote_biology_break(plan)
    assert plan.suggested_actions[0].action_type == "take_biology_break"
    assert plan.suggested_actions[1].action_type == "take_biology_break"


def test_promote_biology_break_survives_sanitization() -> None:
    """Audit fix invariant: ``prepare_plan`` (which runs
    ``sanitize_plan_actions``) must NOT drop the promoted break action.
    """
    from cortex.services.intervention_engine.planner import prepare_plan

    plan = _plan_with_actions()
    promote_biology_break(plan, duration_seconds=240, breathing_pattern="box")
    validation, _commands = prepare_plan(plan, tab_count=10)
    assert validation.is_valid
    types_after = [a.action_type for a in plan.suggested_actions]
    assert "take_biology_break" in types_after
    assert types_after[0] == "take_biology_break"
