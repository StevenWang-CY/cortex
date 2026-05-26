"""
P0 §3.6 — all-done auto-fires ``natural_recovery``.

When every micro-step in the active plan reaches ``status="done"``,
the daemon must:

  1. Mutate the cached plan and rebroadcast ``INTERVENTION_TRIGGER``
     so peer surfaces re-render the strikethrough state.
  2. Invoke ``RestoreManager.engage`` EXACTLY ONCE (latched by
     ``_micro_step_recovery_fired``) — a tail-click after recovery
     must not re-engage an already-closed intervention.
  3. Send ``INTERVENTION_RESTORE`` with ``user_action="natural_recovery"``
     so the dashboard, popup, and VS Code panel converge on the
     close state.

The test uses a minimal stand-in for the daemon assembled directly
out of the real ``CortexDaemon.toggle_micro_step`` coroutine — we
bind it to a lightweight object that carries just the attributes
``toggle_micro_step`` reads.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from cortex.libs.schemas.intervention import (
    InterventionPlan,
    MicroStep,
    UIPlan,
)
from cortex.libs.schemas.ws_message_types import MessageType
from cortex.services.runtime_daemon import CortexDaemon


def _make_plan(intervention_id: str = "int_test") -> InterventionPlan:
    return InterventionPlan(
        intervention_id=intervention_id,
        level="overlay_only",
        situation_summary="placeholder",
        headline="placeholder",
        primary_focus="placeholder",
        micro_steps=[
            MicroStep(text="step a", status="pending"),
            MicroStep(text="step b", status="pending"),
        ],
        ui_plan=UIPlan(),
    )


def _make_fake_daemon(plan: InterventionPlan) -> SimpleNamespace:
    """Assemble the smallest object that satisfies the attributes
    ``CortexDaemon.toggle_micro_step`` reads. Avoids spinning the
    full daemon constructor (which pulls in capture, LLM, redis, …).
    """
    ws_server = MagicMock()
    ws_server.send_message = AsyncMock(return_value=1)
    ws_server.send_restore = AsyncMock(return_value=1)

    restore_manager = MagicMock()
    restore_manager.engage = AsyncMock(
        return_value=SimpleNamespace(
            intervention_id=plan.intervention_id,
            model_dump=lambda mode="json": {
                "intervention_id": plan.intervention_id,
                "user_action": "natural_recovery",
            },
        )
    )

    helpfulness = MagicMock()
    helpfulness.record_user_action = MagicMock()

    recorder = MagicMock()
    recorder.append = MagicMock()

    import asyncio as _asyncio
    fake = SimpleNamespace(
        _ws_server=ws_server,
        _restore_manager=restore_manager,
        _helpfulness=helpfulness,
        _recorder=recorder,
        _active_plan=plan,
        _active_intervention_id=plan.intervention_id,
        _micro_step_recovery_fired=False,
        # Wave-2 P1: serialise toggle vs. F16 plan-swap. The lock
        # lives on the production daemon; the fake needs one too so
        # the ``async with self._micro_step_lock`` body inside
        # ``toggle_micro_step`` doesn't AttributeError.
        _micro_step_lock=_asyncio.Lock(),
    )
    return fake


@pytest.mark.asyncio
async def test_all_done_fires_natural_recovery() -> None:
    """Tick every step → ``RestoreManager.engage`` fires once, plus
    a final ``send_restore`` carrying ``natural_recovery``."""
    plan = _make_plan()
    fake = _make_fake_daemon(plan)

    # Bind the real coroutine to the fake daemon by passing ``fake`` as
    # ``self`` — we want the production logic without the production
    # constructor.
    await CortexDaemon.toggle_micro_step(fake, plan.intervention_id, 0, "done")
    await CortexDaemon.toggle_micro_step(fake, plan.intervention_id, 1, "done")

    # The first tick should NOT engage (only one step done).
    # The second tick should engage exactly once.
    assert fake._restore_manager.engage.call_count == 1
    fake._restore_manager.engage.assert_called_with(plan.intervention_id)

    # Each tick triggers a rebroadcast. send_message is called per
    # tick; engage path adds the natural_recovery restore broadcast.
    assert fake._ws_server.send_message.call_count == 2
    # Inspect the first send_message call to confirm it's an
    # INTERVENTION_TRIGGER rebroadcast.
    first_call_args = fake._ws_server.send_message.call_args_list[0]
    assert first_call_args.args[0] == MessageType.INTERVENTION_TRIGGER.value

    # send_restore fires on the engage path with natural_recovery.
    fake._ws_server.send_restore.assert_awaited_once_with(
        plan.intervention_id, user_action="natural_recovery"
    )


@pytest.mark.asyncio
async def test_tail_click_after_recovery_is_noop() -> None:
    """Once all steps are done and engage() has fired, further toggles
    against the same intervention_id must be silently dropped — the
    plan has been cleared from ``_active_plan`` so the stale lookup
    fails the active-plan guard."""
    plan = _make_plan()
    fake = _make_fake_daemon(plan)

    await CortexDaemon.toggle_micro_step(fake, plan.intervention_id, 0, "done")
    await CortexDaemon.toggle_micro_step(fake, plan.intervention_id, 1, "done")
    engage_calls_after_recovery = fake._restore_manager.engage.call_count

    # Tail click: untick step 1 → should be a no-op because
    # _active_plan was cleared on the engage path.
    await CortexDaemon.toggle_micro_step(fake, plan.intervention_id, 1, "pending")

    assert fake._restore_manager.engage.call_count == engage_calls_after_recovery


@pytest.mark.asyncio
async def test_stale_intervention_id_is_dropped() -> None:
    """A toggle for an intervention_id that does not match the active
    plan must be silently dropped — no broadcast, no engage."""
    plan = _make_plan()
    fake = _make_fake_daemon(plan)

    await CortexDaemon.toggle_micro_step(
        fake, "int_some_other_id", 0, "done"
    )

    fake._ws_server.send_message.assert_not_called()
    fake._restore_manager.engage.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_status_rejected() -> None:
    """An out-of-catalog status string is dropped without mutating
    the plan or broadcasting."""
    plan = _make_plan()
    fake = _make_fake_daemon(plan)
    original_status = plan.micro_steps[0].status

    await CortexDaemon.toggle_micro_step(
        fake, plan.intervention_id, 0, "bogus"
    )

    assert plan.micro_steps[0].status == original_status
    fake._ws_server.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_step_index_out_of_range_rejected() -> None:
    """A step_index past the end of micro_steps is dropped."""
    plan = _make_plan()
    fake = _make_fake_daemon(plan)

    await CortexDaemon.toggle_micro_step(
        fake, plan.intervention_id, 5, "done"
    )

    fake._ws_server.send_message.assert_not_called()
    fake._restore_manager.engage.assert_not_called()


@pytest.mark.asyncio
async def test_single_tick_only_rebroadcasts_no_engage() -> None:
    """A single tick that does NOT complete every step rebroadcasts
    the trigger but must not fire engage()."""
    plan = _make_plan()
    fake = _make_fake_daemon(plan)

    await CortexDaemon.toggle_micro_step(fake, plan.intervention_id, 0, "done")

    fake._ws_server.send_message.assert_awaited_once()
    fake._restore_manager.engage.assert_not_called()
    assert plan.micro_steps[0].status == "done"
    assert plan.micro_steps[0].completed_at is not None
    assert plan.micro_steps[0].started_at is not None
