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
cognitive overwhelm while coding/studying. Your job is to analyze their current \
workspace context and produce a structured intervention plan with ACTIONABLE \
suggestions the user can execute with one click.

Rules:
- Only use the provided context. Do not hallucinate file names, line numbers, URLs, or errors.
- Identify the ONE immediate bottleneck.
- Suppress irrelevant details.
- Return 1-3 concrete micro-steps (not generic advice like "take a break").
- Generate suggested_actions: specific executable actions referencing REAL data from the context.
- For tab actions, use the integer tab_index from the "Tab N:" lines in the context. NEVER fabricate indices.
- tab_index must be within range [0, N-1] where N = number of tabs shown.
- Keep the headline under 15 words.
- Never recommend destructive actions (deleting files, closing unsaved buffers).
- ALWAYS include a "causal_explanation" field: 1-2 sentences explaining WHY you are \
intervening, referencing specific signals (e.g., "Your heart rate rose 18% while \
switching between 4 tabs in 45 seconds").
- Output ONLY valid JSON matching the schema below. No markdown, no preamble.

Output JSON schema:
{
  "situation_summary": "string, 1-2 sentences",
  "primary_focus": "string, the one thing to look at",
  "headline": "string, under 15 words",
  "causal_explanation": "string, 1-2 sentences explaining WHY this intervention was triggered, referencing specific biometric/behavioral signals",
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
  "tone": "direct",
  "suggested_actions": [
    {
      "action_type": "close_tab|group_tabs|bookmark_and_close|open_url|search_error|highlight_tab|save_session|copy_to_clipboard|start_timer",
      "tab_index": 3,
      "target": "search query, URL to open, or session name (NOT for tab targeting)",
      "label": "Short button label",
      "reason": "Why this helps right now",
      "category": "recommended|optional|informational",
      "reversible": true,
      "group_id": "optional grouping key",
      "metadata": {"tab_title": "...", "search_query": "..."}
    }
  ],
  "error_analysis": {
    "error_type": "syntax|import|type|runtime|build|test|other",
    "root_cause": "1-2 sentence root cause",
    "suggested_fix": "concrete code fix or approach",
    "search_query": "pre-crafted search query",
    "relevant_doc_url": "URL if identifiable from context",
    "failing_abstraction": "the specific function/class/module that is failing",
    "symbol_location": "file:line location of the failing symbol",
    "root_cause_category": "type_mismatch|null_reference|missing_import|logic_error|api_misuse|concurrency|config|other",
    "minimal_edit": "smallest code change that fixes the issue"
  },
  "tab_recommendations": {
    "tabs": [
      {"tab_index": 0, "tab_title": "...", "action": "keep|close|group|bookmark_and_close", "reason": "...", "relevance_score": 0.9, "group_name": "optional"}
    ],
    "summary": "Why these recommendations"
  }
}

IMPORTANT: The user will see a PREVIEW of your recommendations before confirming.
Your output must clearly convey WHAT will happen so the user can approve it.
- tab_recommendations: show EVERY tab with keep/close/group and a reason — the user \
sees a preview of "Keep N tabs / Hide N tabs" and confirms with one click.
- error_analysis: show root cause + suggested_fix — the user sees "Here's what I'll do" and confirms.
- suggested_actions: these are the EXECUTABLE actions that run when the user confirms.

Rules for suggested_actions (1-5 actions, or omit if none warranted):
- For close_tab/group_tabs/bookmark_and_close: MUST provide tab_index (integer from context).
- close_tab must have reversible:true (tabs are grouped, not deleted).
- search_error: include search_query in metadata.
- open_url: put the URL in target.
- Only include error_analysis when errors are present in context.
- ALWAYS include tab_recommendations when 4+ tabs are listed in context, regardless of mode.
- For EVERY tab in tab_recommendations, recommend keep/close/group with a reason and relevance_score.
- NEVER assume a tab is irrelevant based solely on its type. AI assistants (Gemini, ChatGPT, \
Claude), video platforms (YouTube), and communication tools (Slack, Discord) CAN be \
task-relevant. Judge relevance by comparing the tab's topic/title against the focus goal.
- A tab classified as [ai_assistant] is a TOOL the user is actively using for research. \
NEVER recommend closing an ai_assistant tab during a focus session — it is almost always \
being used to complement the user's current learning (e.g., asking Gemini about a concept \
from a YouTube lecture). Give it action:"keep" with relevance_score >= 0.9.
- A [video_platform] tab showing educational/lecture/tutorial content related to the focus \
goal MUST be kept with relevance_score >= 0.9. Users often switch between video tutorials \
and AI assistants — both are part of the same learning workflow.
- Active tabs, learning_platform tabs, and goal-relevant ai_assistant tabs MUST always \
get action:"keep" with relevance_score >= 0.8.
- Tabs with type "goal_relevant" MUST get action:"keep" with relevance_score >= 0.95. \
NEVER recommend closing them under any circumstance — they have been verified to match \
the user's focus goal by title keyword analysis.
- relevance_score should reflect topic alignment with the focus goal, NOT just tab type.
- Tabs with last_activated_ago_seconds < 300 (visited in last 5 minutes) should be \
kept with relevance_score >= 0.7 — the user was recently using them. Only close a \
recently-visited tab if it is clearly a distraction (social media, entertainment) \
unrelated to the focus goal.

CRITICAL — specificity requirements:
- For tab_title, you MUST copy the EXACT title from the "Tab N:" lines in the context. \
Do NOT paraphrase, summarize, or use placeholder names like "Distraction Tab". \
Example: if context says 'Tab 3: [distraction] Twitter / X — https://x.com', \
then tab_title must be "Twitter / X", NOT "Distraction Tab".
- Only include error_analysis when actual error text appears in the context (terminal \
errors or editor errors). If there are no errors, OMIT the error_analysis field \
entirely — do NOT generate a placeholder like "No specific errors detected".
- micro_steps must be SPECIFIC to the workspace context. Each step must reference a \
concrete file, tab, error, or action visible in the context. Never generate generic \
advice like "take a break", "breathe", "focus on your task", or "stay focused".
- causal_explanation must reference SPECIFIC data: tab count, switching frequency, \
time in HYPER state, complexity score. Never say "you are feeling overwhelmed" \
without citing numbers from the state data provided.

Valid intervention_type values: "overlay_only", "simplified_workspace", "guided_mode"
Valid tone values: "direct", "supportive", "minimal"
Valid hide_targets: "browser_tabs_except_active", "terminal_lines_before_last_error_block", \
"editor_symbols_except_current_function"
"""

# ---------------------------------------------------------------------------
# Mode-specific user prompt templates
# ---------------------------------------------------------------------------

_DEBUG_ERROR_SUMMARY = """\
The user is debugging errors. Analyze the error output to identify the root cause.

You MUST generate:
- error_analysis with: error_type, root_cause, suggested_fix, and a pre-crafted search_query
- suggested_actions:
  * A "search_error" action with a well-crafted search query in metadata.search_query
  * If a documentation URL is identifiable, an "open_url" action
  * If a relevant StackOverflow tab is already open, a "highlight_tab" action with its tab_index
- If 4+ tabs are listed below, ALSO generate tab_recommendations for each tab (keep/close/group \
with reason and relevance_score) so the user can organize their workspace while debugging.

The user will see a PREVIEW of your plan and confirm before execution.

Focus goal: {goal_hint}

{context}

State: {state} (confidence {confidence:.0%}, dwelling {dwell:.0f}s)
Complexity: {complexity:.2f}
{constraints_text}
"""

_CODE_FOCUS_REDUCTION = """\
The user has too much code visible. Identify which code region matters most \
and what to fold away.

If 4+ browser tabs are listed, ALSO generate tab_recommendations for each tab \
(keep/close/group with reason and relevance_score) and corresponding close_tab \
suggested_actions for tabs clearly unrelated to the focus goal.

The user will see a PREVIEW of your plan and confirm before execution.

Focus goal: {goal_hint}

{context}

State: {state} (confidence {confidence:.0%}, dwelling {dwell:.0f}s)
Complexity: {complexity:.2f}
{constraints_text}
"""

_BROWSER_TAB_REDUCTION = """\
The user has many browser tabs open. Analyze EACH tab listed below against \
the user's current focus goal.

You MUST generate:
- tab_recommendations: For EVERY tab in the context, recommend keep/close/group \
with a reason and relevance_score (0-1). Group related tabs (e.g., all StackOverflow \
tabs about the same topic) under a shared group_name.
- suggested_actions: Generate close_tab actions (with tab_index) for tabs whose \
topic is clearly unrelated to the focus goal, and group_tabs actions for related tabs that should be grouped.

Focus goal: {goal_hint}

{context}

State: {state} (confidence {confidence:.0%}, dwelling {dwell:.0f}s)
Complexity: {complexity:.2f}
{constraints_text}
"""

_MICRO_STEP_PLANNER = """\
The user is overwhelmed and needs concrete next steps. Generate 1-3 specific, \
actionable micro-steps based on the current workspace context.

Generate suggested_actions for any steps that can be automated:
- If there are tabs clearly unrelated to the focus goal, generate close_tab actions with tab_index.
- If there are errors, generate a search_error action.
- If the user should focus on a specific tab, generate a highlight_tab action.
- If 4+ tabs are listed, ALSO generate tab_recommendations for each tab (keep/close/group \
with reason and relevance_score).

The user will see a PREVIEW of your planned actions and confirm before execution.

Focus goal: {goal_hint}

{context}

State: {state} (confidence {confidence:.0%}, dwelling {dwell:.0f}s)
Complexity: {complexity:.2f}
{constraints_text}
"""

_CALM_OVERLAY_WRITER = """\
Produce a short, empathetic, non-patronizing intervention message. The user is \
overwhelmed but still capable — just needs help focusing.

If 4+ browser tabs are listed, generate tab_recommendations for each tab \
(keep/close/group with reason and relevance_score) and corresponding close_tab \
suggested_actions for tabs clearly unrelated to the focus goal using their tab_index.

The user will see a PREVIEW of your recommendations and confirm before execution.

Focus goal: {goal_hint}

{context}

State: {state} (confidence {confidence:.0%}, dwelling {dwell:.0f}s)
Complexity: {complexity:.2f}
{constraints_text}
"""

# ---------------------------------------------------------------------------
# v2.0 Templates — New intervention modes
# ---------------------------------------------------------------------------

_BREATHING_OVERLAY = """\
The user's respiration rate has dropped dangerously low (screen apnea detected). \
They are fixating intensely — likely holding their breath while reading a stack trace \
or complex code.

Generate a CALMING intervention that guides them through a 4-7-8 breathing pattern:
- Inhale for 4 seconds
- Hold for 7 seconds
- Exhale for 8 seconds

The intervention must NOT break their mental model. Use a gentle ambient overlay, \
not a modal dialog. The headline should acknowledge what they're working on.

Include a causal_explanation referencing the specific respiration and blink suppression signals.

Focus goal: {goal_hint}

{context}

State: {state} (confidence {confidence:.0%}, dwelling {dwell:.0f}s)
Complexity: {complexity:.2f}
Respiration: {extra_context}
{constraints_text}
"""

_ACTIVE_RECALL = """\
The user has been passively reading for an extended period (zombie-reading detected). \
They are scrolling slowly with minimal interaction — likely not absorbing the content.

Based on the visible page text provided below, generate a SPECIFIC, TECHNICAL \
fill-in-the-blank question that tests comprehension of the material. The question \
must be directly based on the actual text content, not generic.

Output a JSON object with these additional fields:
- "recall_question": the fill-in-the-blank question (use ___ for the blank)
- "recall_answer": the correct answer
- "recall_context_sentence": the original sentence the question was derived from

The intervention will blur the screen and show the question. The user must answer \
correctly to unblur.

Include a causal_explanation referencing blink rate, scroll velocity, and mouse inactivity.

Visible page text:
{extra_context}

Focus goal: {goal_hint}
State: {state} (confidence {confidence:.0%}, dwelling {dwell:.0f}s)
{constraints_text}
"""

_RABBIT_HOLE_PROMPT = """\
The user has drifted from their session goal. They set out to work on one thing \
but have been deep in unrelated code/content for an extended period.

Goal: {goal_hint}
Current context (what they're actually doing):
{context}

Drift information:
{extra_context}

Generate an intervention that:
1. Acknowledges what they're currently working on (don't dismiss it)
2. Reminds them of the original goal
3. Suggests bringing the goal-relevant file/tab to the front
4. Makes the workspace rearrangement REVERSIBLE (include an undo option)

Include a causal_explanation referencing the alignment score and drift duration.

State: {state} (confidence {confidence:.0%}, dwelling {dwell:.0f}s)
{constraints_text}
"""

_ALIGNMENT_SUMMARY = """\
The user is thrashing between multiple windows/tabs rapidly. Analyze the focus \
transition graph data below to identify what they're trying to accomplish and \
where they're stuck.

Focus transition data:
{extra_context}

You MUST generate:
- A situation_summary that identifies the SPECIFIC pattern (e.g., "bouncing between \
Terminal (OAuth Error 400), auth.ts, and AWS docs")
- An error_analysis if the transitions suggest debugging
- Concrete micro_steps to resolve the underlying confusion
- A causal_explanation referencing switching velocity and specific apps/tabs involved

Focus goal: {goal_hint}

{context}

State: {state} (confidence {confidence:.0%}, dwelling {dwell:.0f}s)
Complexity: {complexity:.2f}
{constraints_text}
"""

_DEEP_BOTTLENECK_DIAGNOSIS = """\
The user is stuck debugging and needs expert-level assistance. Your job is to \
minimize their unblock time by providing the most direct path to resolution.

You MUST:
1. Isolate the FAILING ABSTRACTION (which function/class/module is broken)
2. Locate the RELEVANT SYMBOLS (file:line references from the context)
3. State the LIKELY ROOT CAUSE (be specific, not generic)
4. Provide the MINIMAL EDIT (smallest code change that fixes it)
5. Classify the root_cause_category: type_mismatch, null_reference, missing_import, \
logic_error, api_misuse, concurrency, config, or other

The error_analysis MUST include failing_abstraction, symbol_location, \
root_cause_category, and minimal_edit fields.

Include a causal_explanation referencing error persistence, time spent debugging, \
and workspace complexity signals.

Focus goal: {goal_hint}

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
    # v2.0 templates
    "breathing_overlay": _BREATHING_OVERLAY,
    "active_recall": _ACTIVE_RECALL,
    "rabbit_hole": _RABBIT_HOLE_PROMPT,
    "alignment_summary": _ALIGNMENT_SUMMARY,
    "deep_bottleneck_diagnosis": _DEEP_BOTTLENECK_DIAGNOSIS,
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

    goal_hint = "Not specified"
    if context.current_goal_hint:
        goal_hint = context.current_goal_hint
    elif (
        context.browser_context
        and context.browser_context.focus_goal
    ):
        goal_hint = context.browser_context.focus_goal

    return template.format(
        context=context.to_llm_context(),
        state=state.state,
        confidence=state.confidence,
        dwell=state.dwell_seconds,
        complexity=context.complexity_score,
        constraints_text=constraints_text,
        goal_hint=goal_hint,
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
