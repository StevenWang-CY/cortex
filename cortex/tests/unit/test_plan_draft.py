"""PlanDraft — the model-authored structured-output contract (audit D8, D9).

* D8: the old forced-tool schema was the full ``InterventionPlan``, so the
  model could set daemon-owned fields (``intervention_id``, ``metadata``,
  ``consent_level``, ...). The draft schema must exclude them and forbid
  extras on every object.
* D9: ``{}`` must never validate into a live plan.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from cortex.libs.schemas.intervention import InterventionPlan, SuggestedAction
from cortex.services.llm_engine.parser import validate_intervention_plan
from cortex.services.llm_engine.plan_draft import (
    DAEMON_OWNED_FIELDS,
    DraftActionType,
    PlanDraft,
    draft_to_plan_data,
    structured_output_schema,
)

_ALLOWED_KEYWORDS = {
    "type",
    "properties",
    "required",
    "additionalProperties",
    "enum",
    "items",
    "anyOf",
    "$defs",
    "$ref",
    "description",
}
_FORBIDDEN_KEYWORDS = {
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
}

VALID_DRAFT: dict[str, Any] = {
    "situation_summary": "1 error in main.py",
    "primary_focus": "main.py:10",
    "headline": "Fix the NameError on line 10",
    "causal_explanation": "1 active error pulled focus off the function.",
    "micro_steps": ["Read the NameError", "Define x before use"],
    "hide_targets": ["editor_symbols_except_current_function"],
    "ui_plan": {
        "dim_background": False,
        "show_overlay": True,
        "fold_unrelated_code": True,
        "intervention_type": "simplified_workspace",
    },
    "tone": "supportive",
    "suggested_actions": [
        {
            "action_type": "search_error",
            "tab_index": None,
            "target": "NameError name x is not defined python",
            "label": "Search the error",
            "reason": "Fastest path to the fix",
            "category": "recommended",
            "reversible": True,
            "group_id": None,
            "metadata": [{"key": "search_query", "value": "NameError x python"}],
        }
    ],
    "error_analysis": {
        "error_type": "runtime",
        "root_cause": "x is referenced before assignment in handle_request",
        "suggested_fix": "Define x before the call",
        "search_query": "NameError x python",
        "relevant_doc_url": "",
        "failing_abstraction": "handle_request",
        "symbol_location": "main.py:10",
        "root_cause_category": "logic_error",
        "minimal_edit": "x = compute() above line 10",
    },
    "tab_recommendations": None,
}


def _schema_nodes(node: Any) -> Iterator[dict[str, Any]]:
    """Yield every schema node (skipping the name maps of properties/$defs)."""
    if isinstance(node, list):
        for item in node:
            yield from _schema_nodes(item)
        return
    if not isinstance(node, dict):
        return
    yield node
    for key, value in node.items():
        if key in {"properties", "$defs"}:
            for sub in value.values():
                yield from _schema_nodes(sub)
        else:
            yield from _schema_nodes(value)


def _property_names(schema: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for node in _schema_nodes(schema):
        names.update(node.get("properties", {}).keys())
    return names


def test_schema_excludes_every_daemon_owned_field() -> None:
    schema = structured_output_schema()
    assert DAEMON_OWNED_FIELDS >= {
        "intervention_id",
        "metadata",
        "consent_level",
        "trigger_url",
        "causal_signals",
        "plan_warnings",
        "action_id",
        "catalog_id",
    }
    # Plan-level: none of the daemon-owned names (including ``metadata``).
    top_level = set(schema["properties"])
    assert top_level.isdisjoint(DAEMON_OWNED_FIELDS), top_level & DAEMON_OWNED_FIELDS
    # Nested objects: the only permitted ``metadata`` is the model-authored
    # key/value pair list on a suggested action; every other daemon-owned
    # name is absent everywhere.
    nested_forbidden = DAEMON_OWNED_FIELDS - {"metadata"}
    names = _property_names(schema)
    assert names.isdisjoint(nested_forbidden), names & nested_forbidden
    for node in _schema_nodes(schema):
        props = node.get("properties", {})
        if "metadata" in props:
            assert node is schema["$defs"]["DraftSuggestedAction"]
            assert props["metadata"]["type"] == "array"


def test_every_object_is_closed_and_every_property_required() -> None:
    objects = [
        node
        for node in _schema_nodes(structured_output_schema())
        if node.get("type") == "object" or "properties" in node
    ]
    assert objects
    for node in objects:
        assert node.get("additionalProperties") is False, node
        assert node.get("required") == list(node.get("properties", {}).keys()), node


def test_schema_uses_only_supported_keywords() -> None:
    for node in _schema_nodes(structured_output_schema()):
        keys = set(node.keys())
        assert keys <= _ALLOWED_KEYWORDS, keys - _ALLOWED_KEYWORDS
        assert keys.isdisjoint(_FORBIDDEN_KEYWORDS)


def test_schema_top_level_shape() -> None:
    schema = structured_output_schema()
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == set(PlanDraft.model_fields)
    # Optional analyses are nullable, never absent.
    assert {"type": "null"} in schema["properties"]["error_analysis"]["anyOf"]
    assert {"type": "null"} in schema["properties"]["tab_recommendations"]["anyOf"]
    # Metadata is key/value pairs (the grammar rejects additionalProperties != false).
    action_schema = schema["$defs"]["DraftSuggestedAction"]
    assert action_schema["properties"]["metadata"]["type"] == "array"


def test_schema_is_returned_as_a_fresh_copy() -> None:
    first = structured_output_schema()
    first["properties"].clear()
    assert "situation_summary" in structured_output_schema()["properties"]


def test_draft_fields_are_a_model_authored_subset_of_the_plan() -> None:
    plan_fields = set(InterventionPlan.model_fields)
    draft_fields = set(PlanDraft.model_fields)
    assert draft_fields <= plan_fields
    assert draft_fields.isdisjoint(DAEMON_OWNED_FIELDS)
    assert "level" not in draft_fields  # derived from ui_plan.intervention_type


def test_action_type_vocabulary_matches_suggested_action() -> None:
    plan_vocab = set(get_args(SuggestedAction.model_fields["action_type"].annotation))
    assert set(get_args(DraftActionType)) == plan_vocab


def test_empty_object_is_rejected_everywhere() -> None:
    with pytest.raises(ValidationError):
        PlanDraft.model_validate({})
    assert validate_intervention_plan({}) is None


@pytest.mark.parametrize(
    "extra",
    [
        {"intervention_id": "int_evil"},
        {"metadata": {"source": "llm"}},
        {"consent_level": "autonomous_act"},
        {"trigger_url": "https://evil.example"},
        {"plan_warnings": []},
        {"level": "guided_mode"},
    ],
)
def test_daemon_owned_or_unknown_top_level_keys_are_forbidden(extra: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        PlanDraft.model_validate({**VALID_DRAFT, **extra})


def test_nested_extras_are_forbidden() -> None:
    bad_action = dict(VALID_DRAFT["suggested_actions"][0], action_id="act_evil")
    with pytest.raises(ValidationError):
        PlanDraft.model_validate({**VALID_DRAFT, "suggested_actions": [bad_action]})
    bad_ui = dict(VALID_DRAFT["ui_plan"], max_visible_lines=400)
    with pytest.raises(ValidationError):
        PlanDraft.model_validate({**VALID_DRAFT, "ui_plan": bad_ui})


def test_degenerate_drafts_are_detected() -> None:
    assert PlanDraft.model_validate(VALID_DRAFT).is_degenerate() is False
    assert PlanDraft.model_validate({**VALID_DRAFT, "headline": " "}).is_degenerate() is True
    assert PlanDraft.model_validate({**VALID_DRAFT, "micro_steps": []}).is_degenerate() is True
    assert PlanDraft.model_validate({**VALID_DRAFT, "micro_steps": [" "]}).is_degenerate()
    assert PlanDraft.model_validate({**VALID_DRAFT, "situation_summary": ""}).is_degenerate()


def test_draft_to_plan_data_round_trips_into_a_validated_plan() -> None:
    draft = PlanDraft.model_validate(VALID_DRAFT)
    data = draft_to_plan_data(draft)
    assert set(data).isdisjoint(DAEMON_OWNED_FIELDS)
    assert data["level"] == "simplified_workspace"
    plan = validate_intervention_plan(data)
    assert plan is not None
    assert plan.level == "simplified_workspace"
    assert plan.headline == "Fix the NameError on line 10"
    assert [step.text for step in plan.micro_steps] == [
        "Read the NameError",
        "Define x before use",
    ]
    assert plan.suggested_actions[0].metadata == {"search_query": "NameError x python"}
    assert plan.suggested_actions[0].action_id.startswith("act_")
    assert plan.error_analysis is not None
    assert plan.error_analysis.root_cause_category == "logic_error"
    assert plan.tab_recommendations is None
    # Daemon-owned fields keep their local defaults.
    assert plan.intervention_id.startswith("int_")
    assert plan.metadata == {}
    assert plan.consent_level == "suggest"
    assert plan.trigger_url is None
    assert plan.causal_signals == []
    assert plan.plan_warnings == []


def test_draft_to_plan_data_drops_blank_steps_and_omits_null_analyses() -> None:
    draft = PlanDraft.model_validate(
        {**VALID_DRAFT, "micro_steps": ["  Read it ", "", "  "], "error_analysis": None}
    )
    data = draft_to_plan_data(draft)
    assert data["micro_steps"] == ["Read it"]
    assert "error_analysis" not in data
    assert "tab_recommendations" not in data
