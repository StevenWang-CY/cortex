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

from cortex.libs.schemas.intervention import InterventionPlan, UIPlan


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
