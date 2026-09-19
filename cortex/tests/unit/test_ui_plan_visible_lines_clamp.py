"""``UIPlan.max_visible_lines`` must stay inside its own declared contract.

The planner halves the client's requested ``max_visible_lines`` so VS Code can
size its fold window per plan. It clamped the result to a floor of 5, but
``UIPlan.max_visible_lines`` declares ``ge=10, le=400`` — and ``UIPlan`` has no
``validate_assignment``, so assigning 5 was stored and serialised silently
rather than raising. ``SimplificationConstraints.max_visible_lines`` allows
10-200 and its only production source is client-supplied, so any caller asking
for 10-19 visible lines received a plan four below the field's own floor.
"""

from __future__ import annotations

import pytest

from cortex.libs.schemas.intervention import UIPlan
from cortex.services.llm_engine.anthropic_planner import (
    _ui_plan_visible_line_bounds,
)


def test_bounds_are_read_from_the_schema_not_restated() -> None:
    bounds = _ui_plan_visible_line_bounds()
    # Whatever the schema says is what the clamp must use; if the field's
    # constraints change, this test changes with them rather than pinning a
    # literal that would drift.
    declared_low = None
    declared_high = None
    field = UIPlan.model_fields["max_visible_lines"]
    for item in field.metadata:
        if getattr(item, "ge", None) is not None:
            declared_low = item.ge
        if getattr(item, "le", None) is not None:
            declared_high = item.le
    assert bounds.low == declared_low
    assert bounds.high == declared_high


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        (10, 10),   # 10 // 2 == 5, below the floor — the reported defect
        (19, 10),   # 19 // 2 == 9, still below
        (20, 10),   # exactly the floor
        (80, 40),
        (200, 100),
        (100_000, 400),  # above the ceiling
    ],
)
def test_halved_constraint_is_clamped_into_the_contract(
    requested: int, expected: int,
) -> None:
    bounds = _ui_plan_visible_line_bounds()
    assert bounds.clamp(requested // 2) == expected


def test_a_clamped_value_actually_validates() -> None:
    """The point of the clamp: the result must survive the model's own rules."""
    bounds = _ui_plan_visible_line_bounds()
    value = bounds.clamp(10 // 2)
    plan = UIPlan(max_visible_lines=value)
    assert plan.max_visible_lines == value

    # And the pre-fix value does not, which is why silent assignment mattered.
    with pytest.raises(Exception):
        UIPlan(max_visible_lines=5)
