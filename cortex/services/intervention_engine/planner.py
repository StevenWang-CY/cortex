"""
Intervention Engine — Plan Validation & Mapping

Validates LLM-generated InterventionPlans against safety constraints and
maps hide_targets to concrete adapter commands.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from cortex.libs.schemas.intervention import (
    InterventionPlan,
    SuggestedAction,
    UIPlan,
)

# ---------------------------------------------------------------------------
# Adapter command types
# ---------------------------------------------------------------------------


@dataclass
class AdapterCommand:
    """A concrete command to send to a workspace adapter."""

    adapter: str  # "editor", "browser", "terminal", "overlay"
    action: str
    params: dict[str, object] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# hide_targets → adapter command mapping
# ---------------------------------------------------------------------------

_HIDE_TARGET_MAP: dict[str, AdapterCommand] = {
    "browser_tabs_except_active": AdapterCommand(
        adapter="browser",
        action="hide_tabs_except_active",
    ),
    "terminal_lines_before_last_error_block": AdapterCommand(
        adapter="terminal",
        action="collapse_before_error",
    ),
    "editor_symbols_except_current_function": AdapterCommand(
        adapter="editor",
        action="fold_except_current",
    ),
}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    """Result of plan validation."""

    is_valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def validate_plan(plan: InterventionPlan) -> ValidationResult:
    """
    Validate an InterventionPlan against safety and quality constraints.

    Checks:
    - headline < 15 words
    - 1-3 micro_steps
    - No destructive actions (delete, close, remove permanently, discard)
    - Required fields present
    - Valid level

    Returns:
        ValidationResult with is_valid flag, errors, and warnings.
    """
    errors: list[str] = []
    warnings: list[str] = []

    # Check headline length
    word_count = len(plan.headline.split())
    if word_count > 15:
        errors.append(f"headline has {word_count} words (max 15)")

    # Check micro_steps count
    step_count = len(plan.micro_steps)
    if step_count < 1:
        errors.append("must have at least 1 micro_step")
    elif step_count > 3:
        errors.append(f"has {step_count} micro_steps (max 3)")

    # Check for destructive actions (keep as warning; dropped during sanitization).
    if plan.is_destructive:
        warnings.append("plan contains destructive actions (dropped)")

    # Check required fields are non-empty
    if not plan.situation_summary.strip():
        errors.append("situation_summary is empty")
    if not plan.primary_focus.strip():
        errors.append("primary_focus is empty")

    # Check valid level
    valid_levels = {"overlay_only", "simplified_workspace", "guided_mode"}
    if plan.level not in valid_levels:
        errors.append(f"invalid level '{plan.level}'")

    # Validate suggested_actions
    for action in plan.suggested_actions:
        if action.action_type in ("close_tab", "group_tabs", "bookmark_and_close"):
            if not action.reversible:
                errors.append(
                    f"action {action.action_id} ({action.action_type}) must be reversible"
                )
        if not action.label:
            warnings.append(f"action {action.action_id} has empty label")
    if len(plan.suggested_actions) > 10:
        warnings.append(
            f"excessive suggested_actions ({len(plan.suggested_actions)}), capped at 10"
        )

    # Warnings for unusual but not invalid conditions
    if not plan.hide_targets:
        warnings.append("no hide_targets specified")

    for target in plan.hide_targets:
        if target not in _HIDE_TARGET_MAP:
            warnings.append(f"unknown hide_target '{target}'")

    return ValidationResult(
        is_valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
    )


def sanitize_plan_actions(
    plan: InterventionPlan,
    *,
    tab_count: int | None = None,
) -> list[str]:
    """
    Drop invalid suggested actions in-place and return non-fatal warnings.
    """
    warnings: list[str] = []
    sanitized = []
    for action in plan.suggested_actions:
        if action.tab_index is not None and tab_count is not None:
            if action.tab_index < 0 or action.tab_index >= tab_count:
                warnings.append(
                    f"dropped action {action.action_id}: tab_index {action.tab_index} out of range"
                )
                continue
        if action.action_type in {"close_tab", "bookmark_and_close"} and not action.reversible:
            warnings.append(
                f"dropped action {action.action_id}: close action must be reversible"
            )
            continue
        if any(tok in action.label.lower() for tok in ("discard", "delete project", "delete file")):
            warnings.append(
                f"dropped action {action.action_id}: destructive label content"
            )
            continue
        sanitized.append(action)
    plan.suggested_actions = sanitized
    return warnings


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def map_hide_targets(plan: InterventionPlan) -> list[AdapterCommand]:
    """
    Map a plan's hide_targets to concrete adapter commands.

    Unknown targets are silently skipped (they'll appear as warnings
    in validation).

    Returns:
        List of AdapterCommand objects to send to workspace adapters.
    """
    commands: list[AdapterCommand] = []

    for target in plan.hide_targets:
        cmd = _HIDE_TARGET_MAP.get(target)
        if cmd is not None:
            commands.append(cmd)

    # Add UI plan commands based on the plan's ui_plan
    if plan.ui_plan.dim_background:
        commands.append(
            AdapterCommand(adapter="overlay", action="dim_background")
        )
    if plan.ui_plan.show_overlay:
        commands.append(
            AdapterCommand(
                adapter="overlay",
                action="show_overlay",
                params={
                    "headline": plan.headline,
                    "micro_steps": [s.model_dump(mode="json") for s in plan.micro_steps],
                    "situation_summary": plan.situation_summary,
                    "tone": plan.tone,
                },
            )
        )
    if plan.ui_plan.fold_unrelated_code:
        # Only add if not already added via hide_targets
        has_fold = any(
            c.adapter == "editor" and c.action == "fold_except_current"
            for c in commands
        )
        if not has_fold:
            commands.append(
                AdapterCommand(adapter="editor", action="fold_except_current")
            )

    return commands


def promote_biology_break(
    plan: InterventionPlan,
    *,
    duration_seconds: int = 240,
    breathing_pattern: Literal["box", "4-7-8", "coherent"] | None = None,
    audio_cue: bool = True,
    reason: str = "stress_integral_crossed_threshold",
) -> InterventionPlan:
    """Mutate ``plan`` so ``take_biology_break`` is the primary action.

    Called by the daemon when :meth:`StressIntegralTracker.should_break`
    is True. The function:

    * prepends a ``take_biology_break`` :class:`SuggestedAction` with
      the configured metadata (the desktop shell reads
      ``metadata.duration_seconds`` etc. to drive the overlay),
    * downgrades any peer ``recommended`` actions to ``optional`` so
      the UI's single-CTA preview→confirm→execute path elevates the
      break,
    * forces ``ui_plan.intervention_type = "overlay_only"`` so the
      desktop shell renders the break overlay rather than the
      simplified workspace,
    * rewrites the headline / primary_focus to a deterministic
      copy (the LLM is free to keep its own text in the situation
      summary).

    The plan object is mutated in place and also returned for
    composability with planner pipelines.
    """
    metadata: dict[str, object] = {
        "duration_seconds": int(duration_seconds),
        "breathing_pattern": breathing_pattern or "auto",
        "audio_cue": bool(audio_cue),
        "reason": str(reason)[:120],
    }
    break_action = SuggestedAction(
        action_type="take_biology_break",
        target="",
        label=f"Take a {max(1, duration_seconds // 60)}-minute break",
        reason="Your HRV has been suppressed long enough that the daemon "
               "recommends a guided breathing reset.",
        category="recommended",
        reversible=True,
        metadata=metadata,
    )

    # Demote peer recommended actions so the single-CTA UI surfaces the
    # break, not e.g. a tab-close as the primary action.
    for action in plan.suggested_actions:
        if action.category == "recommended":
            action.category = "optional"
    plan.suggested_actions = [break_action, *plan.suggested_actions]

    # Force overlay-only — the simplified_workspace path would hide tabs
    # while the breathing overlay runs, which is jarring.
    plan.ui_plan = UIPlan(
        dim_background=True,
        show_overlay=True,
        fold_unrelated_code=False,
        intervention_type="overlay_only",
        max_visible_lines=plan.ui_plan.max_visible_lines,
    )
    plan.level = "overlay_only"
    plan.headline = f"Take a {max(1, duration_seconds // 60)}-minute break."
    plan.primary_focus = "Your breath"
    plan.tone = "supportive"
    plan.consent_level = "suggest"
    return plan


def prepare_plan(
    plan: InterventionPlan,
    *,
    tab_count: int | None = None,
) -> tuple[ValidationResult, list[AdapterCommand]]:
    """
    Validate and map a plan in one call.

    Returns:
        Tuple of (validation_result, adapter_commands).
        Commands are empty if validation fails.
    """
    dropped_warnings = sanitize_plan_actions(plan, tab_count=tab_count)
    result = validate_plan(plan)
    if dropped_warnings:
        result.warnings.extend(dropped_warnings)
    if not result.is_valid:
        return result, []
    commands = map_hide_targets(plan)
    return result, commands
