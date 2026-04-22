"""
Intervention Engine — Plan Validation & Mapping

Validates LLM-generated InterventionPlans against safety constraints and
maps hide_targets to concrete adapter commands.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cortex.libs.schemas.intervention import InterventionPlan

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
                    "micro_steps": plan.micro_steps,
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
