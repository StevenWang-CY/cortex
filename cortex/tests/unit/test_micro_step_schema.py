"""
P0 §3.6 — :class:`MicroStep` schema validation + legacy-string coercion.

The promotion from ``list[str]`` to ``list[MicroStep]`` must remain
backwards-compatible because (a) the LLM may still emit the legacy
string form for several releases, and (b) old daemons still in the
field broadcast the legacy shape on the wire. The validator in
:class:`InterventionPlan` lifts every legacy entry into
``MicroStep(text=…, status="pending")``; the tests below pin that
invariant plus the new validation surface (bad status, empty text,
length cap).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cortex.libs.schemas.intervention import (
    InterventionPlan,
    MicroStep,
    UIPlan,
)


def _build_plan(micro_steps: object) -> InterventionPlan:
    """Construct a minimal :class:`InterventionPlan` with the given
    ``micro_steps`` payload. All other required fields take placeholder
    values so the tests focus on the steps field exclusively."""
    return InterventionPlan(
        level="overlay_only",
        situation_summary="placeholder",
        headline="placeholder",
        primary_focus="placeholder",
        micro_steps=micro_steps,  # type: ignore[arg-type]
        ui_plan=UIPlan(),
    )


def test_legacy_str_list_coerces_to_micro_step_pending() -> None:
    """A ``list[str]`` payload is promoted into ``MicroStep`` entries
    each carrying ``status="pending"``. This is the migration path —
    LLM prompts and older daemons still emit strings."""
    plan = _build_plan(["re-read the error", "open the function"])
    assert len(plan.micro_steps) == 2
    assert all(isinstance(s, MicroStep) for s in plan.micro_steps)
    assert plan.micro_steps[0].text == "re-read the error"
    assert plan.micro_steps[0].status == "pending"
    assert plan.micro_steps[1].text == "open the function"
    assert plan.micro_steps[1].status == "pending"
    assert plan.micro_steps[0].started_at is None
    assert plan.micro_steps[0].completed_at is None


def test_full_micro_step_dict_accepted() -> None:
    """The dict shape with explicit ``status`` is the canonical wire
    form. The validator must accept it as-is without coercion."""
    plan = _build_plan([
        {"text": "step a", "status": "done"},
        {"text": "step b", "status": "pending"},
    ])
    assert len(plan.micro_steps) == 2
    assert plan.micro_steps[0].text == "step a"
    assert plan.micro_steps[0].status == "done"
    assert plan.micro_steps[1].status == "pending"


def test_status_must_be_valid() -> None:
    """``status`` is a strict ``Literal`` — any out-of-catalog value
    must be rejected at parse time so a bogus relay frame cannot
    smuggle nonsense into the active plan."""
    with pytest.raises(ValidationError):
        _build_plan([{"text": "step a", "status": "bogus"}])


def test_empty_text_rejected() -> None:
    """Empty step text is meaningless; ``MicroStep.text`` has
    ``min_length=1`` so the validator must reject it."""
    with pytest.raises(ValidationError):
        _build_plan([{"text": "", "status": "pending"}])


def test_micro_step_max_3() -> None:
    """The schema caps the list at 3 entries (one screen of cognitive
    load); a four-entry payload must fail validation."""
    with pytest.raises(ValidationError):
        _build_plan([
            {"text": "a", "status": "pending"},
            {"text": "b", "status": "pending"},
            {"text": "c", "status": "pending"},
            {"text": "d", "status": "pending"},
        ])


def test_micro_step_min_1() -> None:
    """The lower bound is also enforced — an intervention with zero
    micro-steps is structurally meaningless."""
    with pytest.raises(ValidationError):
        _build_plan([])


def test_mixed_list_falls_through_to_per_item_validation() -> None:
    """A list mixing strings and dicts skips the legacy-coerce path
    (which fires only when every entry is a string) and runs the
    per-item validator instead. The dict entry is accepted; the
    string entry causes a per-item validation error because pydantic
    expects a MicroStep model at that position."""
    with pytest.raises(ValidationError):
        _build_plan([
            {"text": "a", "status": "done"},
            "b",
        ])


def test_done_status_preserves_timestamps() -> None:
    """Explicit ``started_at`` / ``completed_at`` values pass through
    untouched — they are the daemon's lifecycle stamps and must not be
    silently dropped."""
    from datetime import datetime, timezone

    started = datetime(2026, 5, 25, 10, 0, tzinfo=timezone.utc)
    completed = datetime(2026, 5, 25, 10, 5, tzinfo=timezone.utc)
    plan = _build_plan([
        {
            "text": "a",
            "status": "done",
            "started_at": started.isoformat(),
            "completed_at": completed.isoformat(),
        }
    ])
    assert plan.micro_steps[0].started_at == started
    assert plan.micro_steps[0].completed_at == completed
