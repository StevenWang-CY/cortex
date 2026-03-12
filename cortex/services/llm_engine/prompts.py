"""
LLM Engine Prompt Templates

All prompt templates for intervention plan generation, plus selection logic
that picks the right template based on workspace mode and context.
"""

from __future__ import annotations

from cortex.libs.schemas.context import TaskContext
from cortex.libs.schemas.intervention import SimplificationConstraints
from cortex.libs.schemas.state import StateEstimate

# ---------------------------------------------------------------------------
# System prompt (shared across all modes)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are Cortex, a calm and direct workspace assistant. The user is experiencing \
cognitive overwhelm while coding/debugging. Your job is to analyze their current \
workspace context and produce a structured intervention plan.

Rules:
- Only use the provided context. Do not hallucinate file names, line numbers, or errors.
- Identify the ONE immediate bottleneck.
- Suppress irrelevant details.
- Return 1-3 concrete micro-steps (not generic advice like "take a break").
- Specify which workspace elements to hide/fold.
- Keep the headline under 15 words.
- Never recommend destructive actions (deleting files, closing unsaved buffers).
- Output ONLY valid JSON matching the schema below. No markdown, no preamble.

Output JSON schema:
{
  "situation_summary": "string, 1-2 sentences",
  "primary_focus": "string, the one thing to look at",
  "headline": "string, under 15 words",
  "micro_steps": ["step 1", "step 2", "step 3"],
  "hide_targets": [
    "browser_tabs_except_active",
    "terminal_lines_before_last_error_block",
    "editor_symbols_except_current_function"
  ],
  "ui_plan": {
    "dim_background": true,
    "show_overlay": true,
    "fold_unrelated_code": true,
    "intervention_type": "simplified_workspace"
  },
  "tone": "direct"
}

Valid intervention_type values: "overlay_only", "simplified_workspace", "guided_mode"
Valid tone values: "direct", "supportive", "minimal"
Valid hide_targets: "browser_tabs_except_active", "terminal_lines_before_last_error_block", \
"editor_symbols_except_current_function"
"""

# ---------------------------------------------------------------------------
# Mode-specific user prompt templates
# ---------------------------------------------------------------------------

_DEBUG_ERROR_SUMMARY = """\
The user is debugging errors in their code. Condense the terminal error flood \
into a root cause and a single next action.

{context}

State: {state} (confidence {confidence:.0%}, dwelling {dwell:.0f}s)
Complexity: {complexity:.2f}
{constraints_text}
"""

_CODE_FOCUS_REDUCTION = """\
The user has too much code visible. Identify which code region matters most \
and what to fold away.

{context}

State: {state} (confidence {confidence:.0%}, dwelling {dwell:.0f}s)
Complexity: {complexity:.2f}
{constraints_text}
"""

_BROWSER_TAB_REDUCTION = """\
The user has many browser tabs open. Triage tabs by relevance to the current \
coding task and recommend which to hide.

{context}

State: {state} (confidence {confidence:.0%}, dwelling {dwell:.0f}s)
Complexity: {complexity:.2f}
{constraints_text}
"""

_MICRO_STEP_PLANNER = """\
The user is overwhelmed and needs concrete next steps. Generate 1-3 specific, \
actionable micro-steps based on the current workspace context.

{context}

State: {state} (confidence {confidence:.0%}, dwelling {dwell:.0f}s)
Complexity: {complexity:.2f}
{constraints_text}
"""

_CALM_OVERLAY_WRITER = """\
Produce a short, empathetic, non-patronizing intervention message. The user is \
overwhelmed but still capable — just needs help focusing.

{context}

State: {state} (confidence {confidence:.0%}, dwelling {dwell:.0f}s)
Complexity: {complexity:.2f}
{constraints_text}
"""

# ---------------------------------------------------------------------------
# Template registry
# ---------------------------------------------------------------------------

PROMPT_TEMPLATES: dict[str, str] = {
    "debug_error_summary": _DEBUG_ERROR_SUMMARY,
    "code_focus_reduction": _CODE_FOCUS_REDUCTION,
    "browser_tab_reduction": _BROWSER_TAB_REDUCTION,
    "micro_step_planner": _MICRO_STEP_PLANNER,
    "calm_overlay_writer": _CALM_OVERLAY_WRITER,
}


def select_prompt_template(context: TaskContext) -> str:
    """
    Select the best prompt template based on workspace mode and context.

    Selection logic:
    - terminal_errors → debug_error_summary
    - coding_debugging with many errors → debug_error_summary
    - coding_debugging otherwise → code_focus_reduction
    - browsing with many tabs → browser_tab_reduction
    - reading_docs → calm_overlay_writer (mild intervention)
    - mixed / fallback → micro_step_planner
    """
    mode = context.mode

    if mode == "terminal_errors":
        return "debug_error_summary"

    if mode == "coding_debugging":
        # If we have terminal errors or editor errors, focus on debugging
        if context.total_errors > 0:
            return "debug_error_summary"
        return "code_focus_reduction"

    if mode == "browsing":
        if context.browser_context and context.browser_context.tab_count > 5:
            return "browser_tab_reduction"
        return "calm_overlay_writer"

    if mode == "reading_docs":
        return "calm_overlay_writer"

    # mixed or anything else
    return "micro_step_planner"


def build_user_prompt(
    context: TaskContext,
    state: StateEstimate,
    constraints: SimplificationConstraints | None = None,
    *,
    template_name: str | None = None,
) -> str:
    """
    Build the complete user prompt from context, state, and constraints.

    Args:
        context: Workspace context.
        state: User state estimate.
        constraints: Optional simplification constraints.
        template_name: Override automatic template selection.

    Returns:
        Formatted user prompt string.
    """
    if template_name is None:
        template_name = select_prompt_template(context)

    template = PROMPT_TEMPLATES[template_name]

    constraints_text = ""
    if constraints is not None:
        parts = []
        parts.append(f"Max visible tabs: {constraints.max_visible_tabs}")
        parts.append(f"Max visible lines: {constraints.max_visible_lines}")
        if constraints.fold_all_except_current:
            parts.append("Fold all code except current function")
        if constraints.hide_terminal_history:
            parts.append("Hide terminal history except errors")
        constraints_text = "Constraints: " + "; ".join(parts)

    return template.format(
        context=context.to_llm_context(),
        state=state.state,
        confidence=state.confidence,
        dwell=state.dwell_seconds,
        complexity=context.complexity_score,
        constraints_text=constraints_text,
    )


def build_messages(
    context: TaskContext,
    state: StateEstimate,
    constraints: SimplificationConstraints | None = None,
    *,
    template_name: str | None = None,
) -> list[dict[str, str]]:
    """
    Build the full message list for the OpenAI-compatible chat API.

    Returns:
        List of {"role": ..., "content": ...} dicts.
    """
    user_prompt = build_user_prompt(
        context, state, constraints, template_name=template_name
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
