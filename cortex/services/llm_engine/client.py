"""
LLM Engine Client Protocol

Abstract interface for LLM backends. All backends (remote Qwen, local Ollama,
OpenAI-compatible) implement this protocol so the rest of the system can swap
them without code changes.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from cortex.libs.schemas.context import TaskContext
from cortex.libs.schemas.intervention import (
    InterventionPlan,
    SimplificationConstraints,
    SuggestedAction,
    TabRecommendation,
    TabRecommendations,
    UIPlan,
)
from cortex.libs.schemas.state import StateEstimate


@runtime_checkable
class LLMClient(Protocol):
    """Protocol for LLM backends that generate intervention plans."""

    async def generate_intervention_plan(
        self,
        context: TaskContext,
        state: StateEstimate,
        constraints: SimplificationConstraints | None = None,
        *,
        template_name: str | None = None,
        extra_context: str = "",
    ) -> InterventionPlan:
        """
        Generate a structured intervention plan from workspace context and user state.

        Args:
            context: Current workspace context (editor, terminal, browser).
            state: Current user state estimate (HYPER, FLOW, etc.).
            constraints: Optional simplification constraints for the workspace.

        Returns:
            A validated InterventionPlan ready for the intervention engine.

        Raises:
            LLMError: If the LLM call fails after retries and fallback.
        """
        ...

    async def health_check(self) -> bool:
        """Check if the LLM backend is reachable and healthy."""
        ...


class LLMError(Exception):
    """Raised when an LLM call fails irrecoverably."""

    def __init__(self, message: str, *, retries_exhausted: bool = False) -> None:
        super().__init__(message)
        self.retries_exhausted = retries_exhausted


def build_fallback_plan(context: TaskContext | None = None) -> InterventionPlan:
    """
    Build a context-aware rule-based intervention plan when LLM is unavailable.

    Uses actual workspace data (tab titles, error messages, file paths) instead
    of generic placeholders. This is the last-resort fallback.
    """
    summary = "Unable to analyze workspace context. Let's take it one step at a time."
    headline = "Focus on the function you're in"
    primary_focus = "Complete the current task before moving on"
    micro_steps = [
        "Review what you were trying to accomplish",
        "Check the most recent error message",
    ]
    tab_recs: TabRecommendations | None = None
    actions: list[SuggestedAction] = []

    if context is not None:
        if context.terminal_context and context.terminal_context.has_errors:
            summary = f"Error detected: {context.terminal_context.error_summary}"
            headline = "Fix the most recent error first"
            primary_focus = "Address the error in your terminal output"
            micro_steps = [
                "Read the last error message carefully",
                "Check the file and line mentioned in the error",
            ]
        elif context.editor_context and context.editor_context.error_count > 0:
            summary = f"{context.editor_context.error_count} error(s) in {context.editor_context.file_path}"
            headline = "Resolve editor errors one at a time"
            primary_focus = f"Start with the first error in {context.editor_context.file_path}"

        # Generate real tab recommendations from context
        if context.browser_context and context.browser_context.all_tabs:
            tabs = context.browser_context.all_tabs
            active_title = context.browser_context.active_tab_title
            distraction_types = {"distraction", "social"}
            safe_types = {
                "ai_assistant", "documentation", "learning_platform",
                "reference", "code_host", "stackoverflow",
            }

            rec_tabs: list[TabRecommendation] = []
            for i, tab in enumerate(tabs):
                if tab.is_active:
                    rec_tabs.append(TabRecommendation(
                        tab_index=i, tab_title=tab.title,
                        action="keep", reason="Active tab",
                        relevance_score=1.0,
                    ))
                elif tab.tab_type in safe_types:
                    rec_tabs.append(TabRecommendation(
                        tab_index=i, tab_title=tab.title,
                        action="keep", reason=f"{tab.tab_type} tab",
                        relevance_score=0.8,
                    ))
                elif tab.tab_type in distraction_types:
                    rec_tabs.append(TabRecommendation(
                        tab_index=i, tab_title=tab.title,
                        action="close", reason="Likely distracting",
                        relevance_score=0.1,
                    ))
                    actions.append(SuggestedAction(
                        action_type="close_tab",
                        tab_index=i,
                        label=f"Close {tab.title}",
                        reason="Likely distracting",
                    ))
                else:
                    rec_tabs.append(TabRecommendation(
                        tab_index=i, tab_title=tab.title,
                        action="keep", reason="May be relevant",
                        relevance_score=0.5,
                    ))

            close_count = sum(1 for r in rec_tabs if r.action == "close")
            tab_recs = TabRecommendations(
                tabs=rec_tabs,
                summary=f"{close_count} distraction tab(s) found among {len(tabs)} open tabs",
            )

            if not summary.startswith("Error"):
                summary = f"{len(tabs)} tabs open, {close_count} appear distracting"
                headline = f"Focus on {active_title[:60]}" if active_title else headline
                primary_focus = "Close distraction tabs to reduce visual clutter"
                if close_count > 0:
                    titles = [r.tab_title for r in rec_tabs if r.action == "close"][:3]
                    micro_steps = [f"Close {t}" for t in titles]
                    if not micro_steps:
                        micro_steps = ["Review open tabs for relevance"]

    return InterventionPlan(
        level="overlay_only",
        situation_summary=summary,
        headline=headline,
        primary_focus=primary_focus,
        micro_steps=micro_steps,
        hide_targets=["editor_symbols_except_current_function"],
        ui_plan=UIPlan(
            dim_background=False,
            show_overlay=True,
            fold_unrelated_code=True,
            intervention_type="overlay_only",
        ),
        tone="supportive",
        tab_recommendations=tab_recs,
        suggested_actions=actions,
    )


class RuleBasedLLMClient:
    """Minimal LLM client that always returns the built-in fallback plan."""

    async def generate_intervention_plan(
        self,
        context: TaskContext,
        state: StateEstimate,
        constraints: SimplificationConstraints | None = None,
        *,
        template_name: str | None = None,
        extra_context: str = "",
    ) -> InterventionPlan:
        return build_fallback_plan(context)

    async def health_check(self) -> bool:
        return True
