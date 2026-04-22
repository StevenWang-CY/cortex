"""Safety regression tests for 0.2.0 planner/parser hardening."""

from __future__ import annotations

from cortex.libs.schemas.context import BrowserContext, TabInfo, TaskContext
from cortex.libs.schemas.intervention import InterventionPlan, SuggestedAction, UIPlan
from cortex.libs.schemas.state import SignalQuality, StateEstimate, StateScores
from cortex.services.intervention_engine.planner import prepare_plan
from cortex.services.llm_engine.parser import verify_causal_explanation


def _make_context() -> TaskContext:
    tabs = [
        TabInfo(tab_id=1, title="Docs", url="https://docs.python.org", tab_type="documentation", is_active=True),
        TabInfo(tab_id=2, title="StackOverflow", url="https://stackoverflow.com/q/1", tab_type="stackoverflow", is_active=False),
    ]
    return TaskContext(
        mode="mixed",
        active_app="chrome",
        current_goal_hint="Fix test failure",
        complexity_score=0.74,
        browser_context=BrowserContext(
            active_tab_title=tabs[0].title,
            active_tab_url=tabs[0].url,
            all_tabs=tabs,
            tab_type_classification={"documentation": 1, "stackoverflow": 1},
        ),
    )


def _make_estimate() -> StateEstimate:
    return StateEstimate(
        state="HYPER",
        confidence=0.91,
        scores=StateScores(flow=0.1, hypo=0.05, hyper=0.8, recovery=0.05),
        signal_quality=SignalQuality(physio=0.8, kinematics=0.8, telemetry=0.9),
        timestamp=1234.0,
        dwell_seconds=35.0,
    )


def test_prepare_plan_drops_invalid_tab_actions_instead_of_failing():
    plan = InterventionPlan(
        level="simplified_workspace",
        situation_summary="Too many tabs and context switching.",
        headline="Trim workspace",
        primary_focus="Fix failing assertion",
        micro_steps=["Keep docs", "Close noisy tabs"],
        hide_targets=["browser_tabs_except_active"],
        ui_plan=UIPlan(dim_background=True, show_overlay=True, fold_unrelated_code=False),
        suggested_actions=[
            SuggestedAction(action_type="close_tab", tab_index=9, label="Close unrelated tab"),
            SuggestedAction(action_type="highlight_tab", tab_index=0, label="Focus docs"),
        ],
    )

    result, _commands = prepare_plan(plan, tab_count=2)

    assert result.is_valid is True
    assert any("out of range" in w for w in result.warnings)
    assert len(plan.suggested_actions) == 1
    assert plan.suggested_actions[0].action_type == "highlight_tab"


def test_causal_explanation_falls_back_when_not_grounded():
    ctx = _make_context()
    plan = InterventionPlan(
        level="overlay_only",
        situation_summary="Signal mismatch.",
        headline="Regain focus",
        primary_focus="Return to current task",
        micro_steps=["Keep one tab"],
        hide_targets=[],
        ui_plan=UIPlan(dim_background=True, show_overlay=True, fold_unrelated_code=False),
        causal_explanation="Your parasympathetic index is 88 with 12 spikes.",
    )

    verified = verify_causal_explanation(plan, ctx)

    assert verified != plan.causal_explanation
    assert str(ctx.browser_context.tab_count) in verified or f"{ctx.complexity_score:.2f}" in verified


def test_estimate_helper_smoke():
    # Ensures the minimal estimate fixture remains schema-valid for parser tests.
    est = _make_estimate()
    assert est.state == "HYPER"
