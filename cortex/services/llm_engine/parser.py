"""
LLM Engine Response Parser

Fault-tolerant JSON parsing for LLM output. Handles common malformations:
- Missing closing braces
- Trailing commas
- Markdown code fences
- Preamble text before JSON
- Unescaped quotes (best-effort)

Validates parsed output against Pydantic InterventionPlan schema.
Falls back to rule-based plan after 2 failed parse attempts.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

from cortex.libs.schemas.context import TaskContext
from cortex.libs.schemas.intervention import InterventionPlan
from cortex.services.context_engine.tab_classifier import classify_tab


def parse_llm_response(raw: str) -> dict[str, Any] | None:
    """
    Attempt to parse raw LLM output into a JSON dict.

    Applies a chain of repair strategies:
    1. Direct parse
    2. Strip markdown fences and preamble
    3. Fix trailing commas
    4. Balance braces/brackets
    5. Extract first JSON object via regex

    Returns:
        Parsed dict on success, None on failure.
    """
    if not raw or not raw.strip():
        return None

    text = raw.strip()

    # Strategy 1: direct parse
    result = _try_parse(text)
    if result is not None:
        return result

    # Strategy 2: strip markdown fences and preamble
    cleaned = _strip_wrapping(text)
    if cleaned != text:
        result = _try_parse(cleaned)
        if result is not None:
            return result

    # Strategy 3: fix trailing commas
    fixed = _fix_trailing_commas(cleaned)
    if fixed != cleaned:
        result = _try_parse(fixed)
        if result is not None:
            return result

    # Strategy 4: balance braces
    balanced = _balance_braces(fixed)
    if balanced != fixed:
        result = _try_parse(balanced)
        if result is not None:
            return result

    # Strategy 5: regex extract first JSON object
    extracted = _extract_json_object(text)
    if extracted is not None and extracted != text:
        result = _try_parse(extracted)
        if result is not None:
            return result
        # Also try repair on the extracted text
        repaired = _balance_braces(_fix_trailing_commas(extracted))
        result = _try_parse(repaired)
        if result is not None:
            return result

    return None


def validate_intervention_plan(data: dict[str, Any]) -> InterventionPlan | None:
    """
    Validate a parsed dict against the InterventionPlan schema.

    Applies lightweight normalization before validation:
    - Ensures ui_plan is a UIPlan object
    - Infers level from ui_plan.intervention_type if missing
    - Provides defaults for optional fields

    Returns:
        InterventionPlan on success, None on validation failure.
    """
    try:
        normalized = _normalize_plan_data(data)
        plan = InterventionPlan(**normalized)
        return plan
    except (ValidationError, TypeError, KeyError):
        return None


def parse_and_validate(raw: str) -> InterventionPlan | None:
    """
    Parse raw LLM output and validate against schema in one call.

    Returns:
        Validated InterventionPlan or None.
    """
    data = parse_llm_response(raw)
    if data is None:
        return None
    return validate_intervention_plan(data)


# ---------------------------------------------------------------------------
# Post-parse enrichment — cross-reference LLM output with actual context
# ---------------------------------------------------------------------------

# Phrases that indicate the LLM generated a placeholder instead of real data
_GENERIC_ERROR_PHRASES = [
    "no specific errors",
    "no errors detected",
    "not applicable",
    "no error",
    "none detected",
    "no issues found",
    "no errors found",
    "n/a",
]

_GENERIC_STEP_PHRASES = [
    "take a moment to breathe",
    "take a break",
    "focus on your current task",
    "focus on the active tab",
    "focus on your active",
    "continue focusing",
    "focus on the task at hand",
    "stay focused",
    "keep going",
    "take a deep breath",
    "reset your focus",
    "close unnecessary",
    "close distraction",
    "close the distraction",
    "review open tabs",
    "reduce visual clutter",
    "focus on the current task",
    "workspace analysis complete",
    "review the current error or task",
]


_GENERIC_REASON_PHRASES = [
    "not essential for",
    "not relevant to",
    "not related to",
    "may be distracting",
    "could be a distraction",
    "is a distraction",
    "not needed for",
    "unrelated to your",
    "distracting you from",
    "not useful for",
]


def enrich_plan_with_context(
    plan: InterventionPlan,
    context: TaskContext,
) -> InterventionPlan:
    """
    Enrich an LLM-generated plan with real data from the workspace context.

    ALWAYS overwrites LLM-generated tab titles and action labels with real data
    from context when tab_index is valid. The LLM cannot be trusted to copy
    exact tab titles — it generates placeholders like "Distraction Tab",
    "Another Distraction Tab", etc.
    """
    tabs = (
        context.browser_context.all_tabs
        if context.browser_context
        else []
    )

    # --- ALWAYS replace tab_recommendations titles with real titles ---
    if plan.tab_recommendations and tabs:
        for rec in plan.tab_recommendations.tabs:
            if 0 <= rec.tab_index < len(tabs):
                real_title = tabs[rec.tab_index].title
                # Unconditionally use the real title
                rec.tab_title = real_title
                # Replace generic reasons
                if rec.reason and any(
                    phrase in rec.reason.lower()
                    for phrase in _GENERIC_REASON_PHRASES
                ):
                    tab_type = getattr(tabs[rec.tab_index], "tab_type", "")
                    if rec.action in ("close", "bookmark_and_close"):
                        rec.reason = (
                            f"{tab_type.replace('_', ' ').title()} — not related to your current work"
                            if tab_type
                            else "Not related to your current work"
                        )

    # --- Enforce tab-type safety rules via classifier ---
    if plan.tab_recommendations and tabs:
        # Determine if the user is likely debugging/coding
        is_debugging = (
            context.mode in ("coding_debugging", "terminal_errors")
            or context.total_errors > 0
        )
        for rec in plan.tab_recommendations.tabs:
            if 0 <= rec.tab_index < len(tabs):
                tab_url = tabs[rec.tab_index].url
                tab_title = tabs[rec.tab_index].title
                tab_type = classify_tab(tab_url, tab_title)

                # AI assistant tabs are ALWAYS kept — they are tools, not distractions
                if tab_type == "ai_assistant":
                    rec.action = "keep"
                    rec.relevance_score = max(rec.relevance_score, 0.9)

                # Documentation tabs kept when user is debugging/coding
                if tab_type == "documentation" and is_debugging:
                    rec.action = "keep"
                    rec.relevance_score = max(rec.relevance_score, 0.8)

    # --- ALWAYS replace suggested_action labels with real tab titles ---
    if plan.suggested_actions and tabs:
        for action in plan.suggested_actions:
            if action.action_type in ("close_tab", "bookmark_and_close") and action.tab_index is not None:
                if 0 <= action.tab_index < len(tabs):
                    real_title = tabs[action.tab_index].title
                    # Unconditionally use the real title
                    action.label = f"Close {real_title}"
                    # Replace generic reasons on actions too
                    if action.reason and any(
                        phrase in action.reason.lower()
                        for phrase in _GENERIC_REASON_PHRASES
                    ):
                        action.reason = "Likely distracting"

    # --- Drop generic error_analysis ---
    if plan.error_analysis:
        rc = plan.error_analysis.root_cause.lower().strip()
        if any(phrase in rc for phrase in _GENERIC_ERROR_PHRASES) or len(rc) < 10:
            plan.error_analysis = None

    # --- Filter generic micro_steps ---
    if plan.micro_steps:
        filtered = [
            step for step in plan.micro_steps
            if not any(phrase in step.lower() for phrase in _GENERIC_STEP_PHRASES)
        ]
        if filtered:
            plan.micro_steps = filtered
        # If all steps were generic, keep the originals (schema requires ≥1)

    plan.causal_explanation = verify_causal_explanation(plan, context)

    return plan


def verify_causal_explanation(plan: InterventionPlan, context: TaskContext) -> str:
    """
    Ensure causal explanation references at least one observable metric/value.
    """
    explanation = (plan.causal_explanation or "").strip()
    tab_count = context.browser_context.tab_count if context.browser_context else 0
    total_errors = context.total_errors
    complexity = context.complexity_score
    if explanation:
        numeric_tokens = re.findall(r"\d+(?:\.\d+)?", explanation)
        for tok in numeric_tokens:
            try:
                val = float(tok)
            except ValueError:
                continue
            if abs(val - float(tab_count)) < 1e-9:
                return explanation
            if abs(val - float(total_errors)) < 1e-9:
                return explanation
            if abs(val - float(complexity)) < 0.01:
                return explanation

    if tab_count > 0:
        return (
            f"Cortex detected high workspace load with {tab_count} open tabs "
            f"at complexity {complexity:.2f}."
        )
    if total_errors > 0:
        return (
            f"Cortex detected sustained debugging friction with {total_errors} active errors "
            f"at complexity {complexity:.2f}."
        )
    return f"Cortex detected sustained overload patterns at complexity {complexity:.2f}."


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _try_parse(text: str) -> dict[str, Any] | None:
    """Try to parse text as JSON, return dict or None."""
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def _strip_wrapping(text: str) -> str:
    """Remove markdown code fences and any preamble text before the JSON."""
    # Remove ```json ... ``` fences
    stripped = re.sub(r"^```(?:json)?\s*\n?", "", text, flags=re.MULTILINE)
    stripped = re.sub(r"\n?```\s*$", "", stripped, flags=re.MULTILINE)
    stripped = stripped.strip()

    # If text has preamble before first {, extract from first {
    brace_idx = stripped.find("{")
    if brace_idx > 0:
        stripped = stripped[brace_idx:]

    return stripped


def _fix_trailing_commas(text: str) -> str:
    """Remove trailing commas before closing braces/brackets."""
    # ,} → }  and ,] → ]
    fixed = re.sub(r",\s*}", "}", text)
    fixed = re.sub(r",\s*]", "]", fixed)
    return fixed


def _balance_braces(text: str) -> str:
    """Add missing closing braces/brackets to balance the JSON."""
    open_braces = text.count("{") - text.count("}")
    open_brackets = text.count("[") - text.count("]")

    result = text
    if open_brackets > 0:
        result += "]" * open_brackets
    if open_braces > 0:
        result += "}" * open_braces

    return result


def _extract_json_object(text: str) -> str | None:
    """Extract the first complete-looking JSON object from text."""
    start = text.find("{")
    if start == -1:
        return None

    # Find the matching closing brace by counting depth
    depth = 0
    in_string = False
    escape_next = False
    for i in range(start, len(text)):
        c = text[i]
        if escape_next:
            escape_next = False
            continue
        if c == "\\":
            escape_next = True
            continue
        if c == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]

    # No balanced close — return from start to end (will need brace balancing)
    return text[start:]


def _normalize_plan_data(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize parsed JSON data to match InterventionPlan schema expectations."""
    result = dict(data)

    # Ensure ui_plan exists
    if "ui_plan" not in result:
        result["ui_plan"] = {
            "dim_background": False,
            "show_overlay": True,
            "fold_unrelated_code": False,
            "intervention_type": "overlay_only",
        }
    elif isinstance(result["ui_plan"], dict):
        # Ensure intervention_type exists in ui_plan
        result["ui_plan"].setdefault("intervention_type", "overlay_only")

    # Infer level from ui_plan if missing
    if "level" not in result:
        if isinstance(result["ui_plan"], dict):
            result["level"] = result["ui_plan"].get(
                "intervention_type", "overlay_only"
            )
        else:
            result["level"] = "overlay_only"

    # Ensure required string fields have defaults
    result.setdefault("situation_summary", "Workspace analysis complete.")
    result.setdefault("headline", "Focus on the current task")
    result.setdefault("primary_focus", "Address the most pressing issue")
    result.setdefault("micro_steps", ["Review the current error or task"])
    result.setdefault("hide_targets", [])
    result.setdefault("tone", "direct")

    # Clamp micro_steps to 1-3
    steps = result.get("micro_steps", [])
    if isinstance(steps, list):
        if len(steps) == 0:
            result["micro_steps"] = ["Review the current error or task"]
        elif len(steps) > 3:
            result["micro_steps"] = steps[:3]

    # --- Normalize new actionable fields ---

    # suggested_actions: list of dicts, cap at 10
    actions = result.get("suggested_actions")
    if isinstance(actions, list):
        normalized_actions = []
        for action in actions[:10]:
            if isinstance(action, dict) and "action_type" in action:
                action.setdefault("label", action.get("target", "Action")[:200])
                action.setdefault("reason", "")
                action.setdefault("category", "recommended")
                action.setdefault("reversible", True)
                action.setdefault("metadata", {})
                action.setdefault("target", "")
                # Validate tab_index is int or None
                ti = action.get("tab_index")
                if ti is not None:
                    try:
                        action["tab_index"] = int(ti)
                    except (ValueError, TypeError):
                        action["tab_index"] = None
                normalized_actions.append(action)
        result["suggested_actions"] = normalized_actions
    else:
        result.pop("suggested_actions", None)

    # error_analysis: optional dict
    ea = result.get("error_analysis")
    if ea is not None:
        if not isinstance(ea, dict) or "error_type" not in ea or "root_cause" not in ea:
            result.pop("error_analysis", None)
        else:
            ea.setdefault("suggested_fix", "")
            ea.setdefault("search_query", "")
            ea.setdefault("relevant_doc_url", "")

    # tab_recommendations: optional dict with tabs list
    tr = result.get("tab_recommendations")
    if tr is not None:
        if isinstance(tr, dict):
            tabs = tr.get("tabs")
            if isinstance(tabs, list):
                valid_tabs = []
                for t in tabs:
                    if isinstance(t, dict) and "tab_index" in t and "action" in t:
                        try:
                            t["tab_index"] = int(t["tab_index"])
                        except (ValueError, TypeError):
                            continue
                        t.setdefault("tab_title", "")
                        t.setdefault("reason", "")
                        t.setdefault("relevance_score", 0.5)
                        valid_tabs.append(t)
                tr["tabs"] = valid_tabs
            else:
                tr["tabs"] = []
            tr.setdefault("summary", "")
        else:
            result.pop("tab_recommendations", None)

    return result
