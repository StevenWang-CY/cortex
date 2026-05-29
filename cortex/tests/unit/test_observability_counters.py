"""C6 observability: state-engine and intervention-executor counters.

Verifies the two Prometheus counters this slice owns are incremented at
the real callsites:

* ``cortex_state_transitions_total{from_state,to_state}`` — incremented by
  ``ScoreSmoother`` at the confirmed state-transition commit (NOT the raw
  per-frame dominant flicker).
* ``cortex_interventions_applied_total{action_type,consent_level}`` —
  incremented by ``InterventionExecutor`` in every success branch.
"""

from __future__ import annotations

from typing import Any

import pytest

from cortex.libs.config.settings import StateConfig
from cortex.libs.observability.metrics import (
    INTERVENTIONS_APPLIED_TOTAL,
    STATE_TRANSITIONS_TOTAL,
)
from cortex.libs.schemas.intervention import AdapterCommand, InterventionPlan, UIPlan
from cortex.libs.schemas.state import SignalQuality, StateScores, UserState
from cortex.services.intervention_engine.executor import InterventionExecutor
from cortex.services.state_engine.smoother import ScoreSmoother


def _labeled_value(counter: Any, **labels: str) -> float:
    """Read the current value of a labeled Prometheus counter child."""
    return float(counter.labels(**labels)._value.get())  # noqa: SLF001


def _good_quality() -> SignalQuality:
    return SignalQuality(physio=0.8, kinematics=0.7, telemetry=0.9)


def _make_plan() -> InterventionPlan:
    return InterventionPlan(
        level="simplified_workspace",
        situation_summary="Test.",
        headline="Focus",
        primary_focus="Test focus",
        micro_steps=["a", "b"],
        hide_targets=[],
        ui_plan=UIPlan(
            dim_background=False,
            show_overlay=True,
            fold_unrelated_code=False,
            intervention_type="simplified_workspace",
        ),
        tone="direct",
        suggested_actions=[],
    )


class _MockAdapter:
    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail

    async def execute(self, action: str, params: dict[str, Any]) -> bool:
        return not self._fail


def test_state_transition_increments_counter() -> None:
    """A confirmed FLOW→HYPER transition bumps the transition counter."""
    config = StateConfig(
        entry_threshold=0.85,
        exit_threshold=0.70,
        hyper_dwell_seconds=1,
        ema_alpha=0.6,
    )
    smoother = ScoreSmoother(config=config)
    quality = _good_quality()
    hyper_scores = StateScores(flow=0.05, hypo=0.0, hyper=0.99, recovery=0.0)

    before = _labeled_value(
        STATE_TRANSITIONS_TOTAL, from_state="FLOW", to_state="HYPER",
    )

    for i in range(30):
        smoother.update(hyper_scores, quality, timestamp=float(i) * 0.5)

    # Sanity: the smoother actually committed the transition.
    assert smoother.current_state == UserState.HYPER

    after = _labeled_value(
        STATE_TRANSITIONS_TOTAL, from_state="FLOW", to_state="HYPER",
    )
    assert after == before + 1.0, (
        f"expected exactly one flow→hyper increment; before={before} after={after}"
    )


@pytest.mark.asyncio
async def test_intervention_applied_increments_counter_on_success() -> None:
    """A successful adapter dispatch bumps the applied counter once."""
    executor = InterventionExecutor()
    executor._allow_unwired_consent = True  # noqa: SLF001  # test escape hatch
    executor.register_adapter("editor", _MockAdapter())

    plan = _make_plan()
    # fold_except_current → canonical action_type "fold_code"; the
    # simplified_workspace plan resolves to consent_level "suggest".
    commands = [AdapterCommand(adapter="editor", action="fold_except_current")]

    before = _labeled_value(
        INTERVENTIONS_APPLIED_TOTAL, action_type="fold_code", consent_level="suggest",
    )

    mutations = await executor.apply(plan, commands, timestamp=100.0)
    assert mutations[0].success is True

    after = _labeled_value(
        INTERVENTIONS_APPLIED_TOTAL, action_type="fold_code", consent_level="suggest",
    )
    assert after == before + 1.0


@pytest.mark.asyncio
async def test_intervention_applied_not_incremented_on_failure() -> None:
    """A failed adapter dispatch must NOT bump the applied counter."""
    executor = InterventionExecutor()
    executor._allow_unwired_consent = True  # noqa: SLF001
    executor.register_adapter("editor", _MockAdapter(fail=True))

    plan = _make_plan()
    commands = [AdapterCommand(adapter="editor", action="fold_except_current")]

    before = _labeled_value(
        INTERVENTIONS_APPLIED_TOTAL, action_type="fold_code", consent_level="suggest",
    )

    mutations = await executor.apply(plan, commands, timestamp=100.0)
    assert mutations[0].success is False

    after = _labeled_value(
        INTERVENTIONS_APPLIED_TOTAL, action_type="fold_code", consent_level="suggest",
    )
    assert after == before
