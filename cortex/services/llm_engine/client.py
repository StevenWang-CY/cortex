"""
LLM Engine Client Protocol

Abstract interface for LLM backends. All backends (remote Qwen, local Ollama,
OpenAI-compatible) implement this protocol so the rest of the system can swap
them without code changes.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from cortex.libs.schemas.context import TaskContext
from cortex.libs.schemas.intervention import InterventionPlan, SimplificationConstraints, UIPlan
from cortex.libs.schemas.state import StateEstimate


@runtime_checkable
class LLMClient(Protocol):
    """Protocol for LLM backends that generate intervention plans."""

    async def generate_intervention_plan(
        self,
        context: TaskContext,
        state: StateEstimate,
        constraints: SimplificationConstraints | None = None,
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
    Build a generic rule-based intervention plan when LLM is unavailable.

    This is the last-resort fallback used when the LLM server is unreachable
    or returns unparseable responses after all retries.
    """
    summary = "Unable to analyze workspace context. Let's take it one step at a time."
    headline = "Focus on the function you're in"
    primary_focus = "Complete the current task before moving on"
    micro_steps = [
        "Review what you were trying to accomplish",
        "Check the most recent error message",
    ]

    # If we have context, try to make the fallback slightly more useful
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
    )
