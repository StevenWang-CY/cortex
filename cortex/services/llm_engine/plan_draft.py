"""Model-authored plan draft — the structured-output contract of the planner.

:class:`PlanDraft` is the *only* shape the model may author. It carries the
narrative and proposal fields of an
:class:`~cortex.libs.schemas.intervention.InterventionPlan` and nothing
else: every daemon-owned field (``intervention_id``, ``metadata``,
``consent_level``, ``trigger_url``, ``causal_signals``, ``plan_warnings``,
``action_id``, ``catalog_id``, ``max_visible_lines``) is absent here, so
model output can never set it. ``extra="forbid"`` on every draft model plus
``additionalProperties: false`` in the emitted JSON schema make that a
contract enforced on both sides of the wire.

:func:`structured_output_schema` derives the ``output_config.format``
JSON schema from the Pydantic model, tightened to what the Claude API's
structured-output grammar accepts: every object closed
(``additionalProperties: false``), every property required (absence is
expressed as ``null`` / an empty list), and no numeric/string constraint
keywords. :func:`draft_to_plan_data` turns a validated draft into the
plain dict the existing parser normalisation consumes.
"""

from __future__ import annotations

import copy
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

# Fields of InterventionPlan / SuggestedAction / UIPlan that only the
# daemon may set. ``test_plan_draft.py`` asserts none of them appear
# anywhere in the structured-output schema.
DAEMON_OWNED_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "intervention_id",
        "metadata",
        "consent_level",
        "trigger_url",
        "causal_signals",
        "plan_warnings",
        "action_id",
        "catalog_id",
        "max_visible_lines",
    }
)

# Keep in lock-step with ``SuggestedAction.action_type`` in
# ``cortex/libs/schemas/intervention.py`` — ``test_plan_draft.py`` asserts
# the two literal sets are identical.
DraftActionType = Literal[
    "close_tab",
    "group_tabs",
    "bookmark_and_close",
    "open_url",
    "search_error",
    "highlight_tab",
    "save_session",
    "copy_to_clipboard",
    "start_timer",
    "resume_last_active_file",
    "prompt_micro_commit",
    "suggest_movement_break",
    "take_biology_break",
]
HideTarget = Literal[
    "browser_tabs_except_active",
    "terminal_lines_before_last_error_block",
    "editor_symbols_except_current_function",
]
InterventionType = Literal["overlay_only", "simplified_workspace", "guided_mode"]
Tone = Literal["direct", "supportive", "minimal"]
ActionCategory = Literal["recommended", "optional", "informational"]
TabAction = Literal["keep", "close", "group", "bookmark_and_close"]
RootCauseCategory = Literal[
    "type_mismatch",
    "null_reference",
    "missing_import",
    "logic_error",
    "api_misuse",
    "concurrency",
    "config",
    "other",
]


class _DraftModel(BaseModel):
    # Base for every model-authored shape: unknown keys are a hard error.
    # (No docstring on purpose — it would leak into the schema description.)
    model_config = ConfigDict(extra="forbid")


# The structured-output grammar only accepts ``additionalProperties: false``,
# so a free-form ``dict[str, str]`` cannot be expressed; the draft carries
# key/value pairs and :func:`draft_to_plan_data` folds them into the
# ``SuggestedAction.metadata`` dict. Class docstrings become schema
# descriptions the model reads, so they stay short and model-facing.
class DraftMetadataEntry(_DraftModel):
    """One key/value metadata pair of a suggested action."""

    key: str = Field(description="Metadata key, e.g. search_query or tab_title")
    value: str = Field(description="Metadata value")


class DraftUIPlan(_DraftModel):
    dim_background: bool = Field(description="Whether to dim background windows")
    show_overlay: bool = Field(description="Whether to show the intervention overlay")
    fold_unrelated_code: bool = Field(description="Whether to fold unrelated code in the editor")
    intervention_type: InterventionType = Field(description="Type of intervention")


class DraftSuggestedAction(_DraftModel):
    action_type: DraftActionType = Field(description="Type of proposed action")
    tab_index: int | None = Field(
        description=(
            "Integer index from the 'Tab N:' lines in the context for tab "
            "actions; null for actions that do not target a tab"
        )
    )
    target: str = Field(
        description="Search query, URL for open_url, session name, etc.; empty when unused"
    )
    label: str = Field(description="Short human-readable button label")
    reason: str = Field(description="Why this action helps right now")
    category: ActionCategory = Field(description="How strongly recommended")
    reversible: bool = Field(description="Whether the action can be undone")
    group_id: str | None = Field(description="Groups related actions together; null if none")
    metadata: list[DraftMetadataEntry] = Field(
        description="Action-specific metadata pairs (tab_title, search_query, ...)"
    )


class DraftErrorAnalysis(_DraftModel):
    error_type: str = Field(description="Classified error type (syntax, import, type, runtime, ...)")
    root_cause: str = Field(description="1-2 sentence root cause")
    suggested_fix: str = Field(description="Concrete code fix or approach; empty if unknown")
    search_query: str = Field(description="Pre-crafted search query; empty if none")
    relevant_doc_url: str = Field(
        description="URL to relevant documentation if identifiable from context; empty if none"
    )
    failing_abstraction: str = Field(
        description="The specific function/class/module that is failing; empty if unknown"
    )
    symbol_location: str = Field(
        description="file:line location of the failing symbol; empty if unknown"
    )
    root_cause_category: RootCauseCategory = Field(description="Classified root cause category")
    minimal_edit: str = Field(
        description="Smallest code change that fixes the issue; empty if unknown"
    )


class DraftTabRecommendation(_DraftModel):
    tab_index: int = Field(description="Integer index into the context tab list")
    tab_title: str = Field(description="Exact tab title copied from the context")
    action: TabAction = Field(description="Recommended action for this tab")
    reason: str = Field(description="Why this recommendation")
    relevance_score: float = Field(description="Relevance to the focus goal, 0.0 to 1.0")
    group_name: str | None = Field(description="Group name if action is 'group'; otherwise null")


class DraftTabRecommendations(_DraftModel):
    tabs: list[DraftTabRecommendation] = Field(description="One entry per tab in the context")
    summary: str = Field(description="Why these recommendations")


class PlanDraft(_DraftModel):
    """The intervention proposal: a non-authoritative plan for the user."""

    situation_summary: str = Field(description="1-2 sentence summary of the situation")
    primary_focus: str = Field(description="The one thing to look at")
    headline: str = Field(description="Headline for the overlay, under 15 words")
    causal_explanation: str = Field(
        description=(
            "1-2 sentences explaining WHY support may help, referencing specific "
            "observable workspace behaviour"
        )
    )
    micro_steps: list[str] = Field(description="1-3 concrete next steps, most impactful first")
    hide_targets: list[HideTarget] = Field(description="Workspace elements to hide or fold")
    ui_plan: DraftUIPlan = Field(description="UI manipulation instructions")
    tone: Tone = Field(description="Tone of the intervention text")
    suggested_actions: list[DraftSuggestedAction] = Field(
        description="Bounded action proposals (0-5); empty when none are warranted"
    )
    error_analysis: DraftErrorAnalysis | None = Field(
        description="Root-cause analysis when actual error text is in the context; otherwise null"
    )
    tab_recommendations: DraftTabRecommendations | None = Field(
        description="Per-tab keep/close/group triage when 4+ tabs are listed; otherwise null"
    )

    def is_degenerate(self) -> bool:
        """True when the draft carries no usable plan text.

        A structurally valid but empty draft (``""`` headline, no steps)
        must never become a live plan with placeholder text.
        """
        steps = [step for step in self.micro_steps if step.strip()]
        return not (self.headline.strip() and self.situation_summary.strip() and steps)


# JSON-schema keywords the structured-output grammar rejects (numeric and
# string constraints, array bounds) plus the ones that only add tokens.
_STRIPPED_KEYWORDS: Final[frozenset[str]] = frozenset(
    {
        "default",
        "title",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
    }
)


def _tighten(node: Any) -> Any:
    """Recursively close objects, require every property, strip constraints."""
    if isinstance(node, list):
        return [_tighten(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _STRIPPED_KEYWORDS:
            continue
        if key in {"properties", "$defs"}:
            # Maps of name -> schema: the names are data, not keywords.
            out[key] = {name: _tighten(sub) for name, sub in value.items()}
        else:
            out[key] = _tighten(value)
    if out.get("type") == "object" or "properties" in out:
        properties = out.setdefault("properties", {})
        out["required"] = list(properties.keys())
        out["additionalProperties"] = False
    return out


def structured_output_schema() -> dict[str, Any]:
    """JSON schema for ``output_config.format`` derived from :class:`PlanDraft`.

    The result keeps only ``type`` / ``properties`` / ``required`` /
    ``additionalProperties: false`` / ``enum`` / ``items`` / ``anyOf`` /
    ``$defs`` / ``$ref`` / ``description``; every object is closed and
    every property required. A fresh deep copy is returned each call so a
    caller can never mutate the cached model schema.
    """
    raw = copy.deepcopy(PlanDraft.model_json_schema())
    tightened = _tighten(raw)
    assert isinstance(tightened, dict)
    return tightened


def draft_to_plan_data(draft: PlanDraft) -> dict[str, Any]:
    """Convert a validated draft into the dict the parser normalisation takes.

    Only model-authored fields are emitted; ``level`` is derived from
    ``ui_plan.intervention_type`` exactly as the parser would infer it.
    Empty micro-steps are dropped, metadata pairs become a dict, and the
    optional analyses are omitted (not ``None``) when the model declined
    them so the downstream defaults apply.
    """
    data: dict[str, Any] = {
        "situation_summary": draft.situation_summary.strip(),
        "primary_focus": draft.primary_focus.strip(),
        "headline": draft.headline.strip(),
        "causal_explanation": draft.causal_explanation.strip(),
        "micro_steps": [step.strip() for step in draft.micro_steps if step.strip()],
        "hide_targets": list(draft.hide_targets),
        "ui_plan": draft.ui_plan.model_dump(),
        "level": draft.ui_plan.intervention_type,
        "tone": draft.tone,
        "suggested_actions": [
            {
                **action.model_dump(exclude={"metadata"}),
                "metadata": {entry.key: entry.value for entry in action.metadata},
            }
            for action in draft.suggested_actions
        ],
    }
    if draft.error_analysis is not None:
        data["error_analysis"] = draft.error_analysis.model_dump()
    if draft.tab_recommendations is not None:
        data["tab_recommendations"] = draft.tab_recommendations.model_dump()
    return data


__all__ = [
    "DAEMON_OWNED_FIELDS",
    "DraftActionType",
    "DraftErrorAnalysis",
    "DraftMetadataEntry",
    "DraftSuggestedAction",
    "DraftTabRecommendation",
    "DraftTabRecommendations",
    "DraftUIPlan",
    "PlanDraft",
    "draft_to_plan_data",
    "structured_output_schema",
]
