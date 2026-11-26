"""A tab the safety rules force to "keep" must not keep its "Close" action.

``enrich_plan_with_context`` enforces two tab-type rules on a plan's
``tab_recommendations``: AI-assistant tabs are always kept, documentation
tabs are kept while the user is debugging. It flips the recommendation to
``keep`` but leaves the plan's matching ``close_tab`` suggested action in
place, so the proposal tells the user to keep the tab while offering a
one-click "Close <tab>" for it, and the manifest lowered from the plan
carries that close as an authorizable action.
"""

from __future__ import annotations

from cortex.libs.schemas.context import BrowserContext, TabInfo, TaskContext
from cortex.libs.schemas.intervention import (
    InterventionPlan,
    SuggestedAction,
    TabRecommendation,
    TabRecommendations,
    UIPlan,
)
from cortex.services.llm_engine.parser import enrich_plan_with_context


def _context() -> TaskContext:
    tabs = [
        TabInfo(tab_id=1, title="ChatGPT", url="https://chatgpt.com/c/abc", tab_type="other",
                is_active=False),
        TabInfo(tab_id=2, title="Editor docs", url="https://example.com/work", tab_type="other",
                is_active=True),
    ]
    return TaskContext(
        mode="browsing",
        active_app="chrome",
        complexity_score=0.7,
        browser_context=BrowserContext(
            active_tab_title="Editor docs",
            active_tab_url="https://example.com/work",
            all_tabs=tabs,
        ),
    )


def _plan() -> InterventionPlan:
    return InterventionPlan(
        level="overlay_only",
        situation_summary="Two tabs open, one looks unrelated to the task.",
        headline="Close the unrelated tab",
        primary_focus="Keep the work tab",
        micro_steps=["Close the chat tab"],
        ui_plan=UIPlan(
            dim_background=False,
            show_overlay=True,
            fold_unrelated_code=False,
            intervention_type="overlay_only",
        ),
        tone="supportive",
        tab_recommendations=TabRecommendations(
            tabs=[
                TabRecommendation(tab_index=0, tab_title="x", action="close",
                                  reason="Unrelated chat", relevance_score=0.1),
                TabRecommendation(tab_index=1, tab_title="y", action="keep",
                                  reason="Active tab", relevance_score=1.0),
            ],
            summary="1 tab to close",
        ),
        suggested_actions=[
            SuggestedAction(action_type="close_tab", tab_index=0, label="Close x",
                            reason="Unrelated chat"),
        ],
    )


def test_a_kept_ai_assistant_tab_is_not_offered_for_closing() -> None:
    plan = enrich_plan_with_context(_plan(), _context())

    assert plan.tab_recommendations is not None
    kept = {rec.tab_index for rec in plan.tab_recommendations.tabs if rec.action == "keep"}
    assert 0 in kept  # the safety rule fired
    closing = {
        action.tab_index
        for action in plan.suggested_actions
        if action.action_type in ("close_tab", "bookmark_and_close")
    }
    assert not (kept & closing), f"kept tabs still offered for closing: {sorted(kept & closing)}"
