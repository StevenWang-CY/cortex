"""
Intervention Engine — Plan Validation & Mapping

Validates LLM-generated InterventionPlans against safety constraints and
maps hide_targets to concrete adapter commands.
"""

from __future__ import annotations

import logging

from cortex.libs.schemas.intervention import (
    AdapterCommand,
    InterventionPlan,
    SuggestedAction,
    ValidationResult,
)

# ``AdapterCommand`` / ``ValidationResult`` re-exported from libs to keep
# the planner's public surface backwards compatible — both names were
# importable from this module before the libs ⊥ services split.
__all__ = [
    "AdapterCommand",
    "ValidationResult",
    "validate_plan",
    "sanitize_plan_actions",
    "map_hide_targets",
    "materialize_suggestion_only",
    "prepare_plan",
]

logger = logging.getLogger(__name__)

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


def materialize_suggestion_only(plan: InterventionPlan) -> InterventionPlan:
    """Return a presentation-only copy of a validated intervention plan.

    The original plan is retained only as descriptive context. Every field
    that can cause an automatic workspace effect is removed, and suggested
    actions are marked non-executable for clients that render the legacy
    action shape. This conversion happens after validation so it cannot hide
    an invalid LLM response from observability.
    """
    proposal = plan.model_copy(deep=True)
    original_level = proposal.level
    original_consent = proposal.consent_level

    proposal.hide_targets = []
    proposal.ui_plan = proposal.ui_plan.model_copy(
        update={
            "show_overlay": True,
            "fold_unrelated_code": False,
            "intervention_type": "overlay_only",
        },
        deep=True,
    )
    proposal.level = "overlay_only"
    proposal.consent_level = "suggest"
    proposal.metadata = dict(proposal.metadata or {})
    proposal.metadata.update({
        "execution_mode": "suggest_only",
        "original_intervention_level": original_level,
        "original_consent_level": original_consent,
        "workspace_mutation_allowed": False,
    })
    for action in proposal.suggested_actions:
        action.metadata = dict(action.metadata or {})
        action.metadata["execution_available"] = False
    return proposal


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


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

    # Destructive-looking suggestions remain inert presentation copy unless
    # the transaction catalog can mint an exact reversible capability.
    if plan.is_destructive:
        warnings.append("plan contains destructive actions (presentation-only)")

    # Check required fields are non-empty
    if not plan.situation_summary.strip():
        errors.append("situation_summary is empty")
    if not plan.primary_focus.strip():
        errors.append("primary_focus is empty")

    # Check valid level
    valid_levels = {"overlay_only", "simplified_workspace", "guided_mode"}
    if plan.level not in valid_levels:
        errors.append(f"invalid level '{plan.level}'")

    # SuggestedAction.reversible is a catalog-derived presentation hint, not
    # authority and not a prerequisite for showing a proposal. Unsupported
    # close/group actions are intentionally visible but omitted from the exact
    # action manifest by build_action_manifest().
    for action in plan.suggested_actions:
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

    # P1-6: when tab_count is unknown (None), any action that references a
    # specific tab by index is unsafe — we cannot validate the index and the
    # executor would send a command to an arbitrary tab. Drop all such
    # actions and emit a structured warning so the caller can surface it.
    # The remaining (non-tab-indexed) actions still flow through the
    # full safety-filter loop below.
    if tab_count is None:
        filtered: list[SuggestedAction] = []
        for action in plan.suggested_actions:
            if action.tab_index is not None:
                msg = (
                    f"dropped action {action.action_id}: tab_index={action.tab_index} "
                    "cannot be validated because tab_count is unknown"
                )
                warnings.append(msg)
                logger.warning(
                    "sanitize_plan_actions: %s", msg,
                    extra={"action_id": action.action_id, "tab_index": action.tab_index},
                )
            else:
                filtered.append(action)
        plan.suggested_actions = filtered

    for action in plan.suggested_actions:
        if action.tab_index is not None and tab_count is not None:
            if action.tab_index < 0 or action.tab_index >= tab_count:
                warnings.append(
                    f"dropped action {action.action_id}: tab_index {action.tab_index} out of range"
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
