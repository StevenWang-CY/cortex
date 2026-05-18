"""F05 — Apply-intervention waits for client confirmation.

The pre-F05 adapter optimistically reported every dispatched action as
``success=True``. This test exercises the new contract:

* ``daemon.await_apply_confirmation`` returns ``confirmed=True`` when the
  client's ``INTERVENTION_APPLIED`` ack arrives.
* Returns ``confirmed=False`` (and ``timed_out=True``) when no ack
  arrives within the timeout.
* A partial ack (some actions failed) surfaces the per-action breakdown.
* Daemon restart drops any in-flight future and the next ack is a no-op.
* The future is resolved exactly once even if ack + timeout race.

Run with: ``pytest cortex/tests/integration/test_apply_intervention_confirmation.py``
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from cortex.libs.schemas.intervention import InterventionApplyResult


class _MinimalDaemon:
    """A test scaffold that exercises the F05 future-tracking machinery
    without booting the full ``CortexDaemon``. It mirrors the relevant
    attributes and re-uses the production methods by mixin-style binding.

    Real-system bootup needs a camera, a baseline file, and ML state — the
    test only needs the apply-confirmation contract.
    """

    def __init__(self) -> None:
        self._pending_apply_results: dict[str, asyncio.Future[Any]] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._intervention_applied_seen: set[tuple[str, str]] = set()
        # Minimal recorder stub so await_apply_confirmation doesn't blow up.
        self.recorder_calls: list[tuple[str, dict]] = []

        class _Recorder:
            def __init__(self, owner: _MinimalDaemon) -> None:
                self._owner = owner

            def append(self, event_type: str, payload: dict) -> None:
                self._owner.recorder_calls.append((event_type, payload))

        self._recorder = _Recorder(self)

        # Minimal executor stub so _handle_intervention_applied's late
        # mutation reconcile path does not crash. The contract under test
        # is the future-resolution path; the late-mutation path is a
        # side effect we don't need to exercise here.
        class _ExecutorStub:
            def get_active_mutations(self, _intervention_id: str) -> list:
                return []

        self._executor = _ExecutorStub()

    # Bind the production methods we care about. This is a deliberate
    # micro-mixin so the test exercises real code paths.
    from cortex.services.runtime_daemon import CortexDaemon as _CD

    _spawn_background_task = _CD._spawn_background_task
    await_apply_confirmation = _CD.await_apply_confirmation
    _handle_intervention_applied = _CD._handle_intervention_applied


@pytest.mark.asyncio
async def test_apply_then_ack_confirmed_true():
    daemon = _MinimalDaemon()

    async def _ack_after_delay() -> None:
        await asyncio.sleep(0.05)
        await daemon._handle_intervention_applied(
            {
                "intervention_id": "iv-ok",
                "phase": "apply",
                "success": True,
                "applied_actions": ["a1", "a2"],
                "errors": [],
            }
        )

    asyncio.create_task(_ack_after_delay())
    result = await daemon.await_apply_confirmation(
        "iv-ok", timeout_seconds=2.0, correlation_id="cid-1",
    )
    assert isinstance(result, InterventionApplyResult)
    assert result.confirmed is True
    assert result.timed_out is False
    assert result.applied_actions == ["a1", "a2"]
    assert result.intervention_id == "iv-ok"
    # Session recorder should have an entry for the actual ack outcome.
    assert any(
        event == "intervention_apply_confirmation"
        for event, _ in daemon.recorder_calls
    )


@pytest.mark.asyncio
async def test_no_ack_within_timeout_confirms_false():
    daemon = _MinimalDaemon()

    result = await daemon.await_apply_confirmation(
        "iv-timeout", timeout_seconds=0.1, correlation_id="cid-2",
    )
    assert result.confirmed is False
    assert result.timed_out is True
    assert result.applied_actions == []
    # Pending entry must be cleaned up so future calls start fresh.
    assert "iv-timeout" not in daemon._pending_apply_results


@pytest.mark.asyncio
async def test_partial_ack_surfaces_per_action_breakdown():
    daemon = _MinimalDaemon()

    async def _partial_ack() -> None:
        await asyncio.sleep(0.05)
        await daemon._handle_intervention_applied(
            {
                "intervention_id": "iv-partial",
                "phase": "apply",
                "success": False,
                "applied_actions": ["a1"],
                "errors": ["a2 failed: tab not found"],
            }
        )

    asyncio.create_task(_partial_ack())
    result = await daemon.await_apply_confirmation(
        "iv-partial", timeout_seconds=2.0,
    )
    assert result.confirmed is False
    assert result.timed_out is False
    assert result.applied_actions == ["a1"]
    assert result.errors == ["a2 failed: tab not found"]


@pytest.mark.asyncio
async def test_daemon_restart_loses_inflight_next_ack_noop():
    """Simulate a daemon restart: clear pending futures, then a delayed ack
    for the same intervention_id must be treated as a no-op (existing
    dedup logic in ``_handle_intervention_applied`` swallows it)."""
    daemon = _MinimalDaemon()

    # Pretend an apply had a pending future, then the daemon restarted —
    # the future is resolved to confirmed=False before the future awaiter
    # observes the new state. Clear the pending dict as ``stop()`` would.
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    daemon._pending_apply_results["iv-restart"] = future
    # Resolve it as restart would (timeout=True).
    future.set_result(
        InterventionApplyResult(
            intervention_id="iv-restart", confirmed=False, timed_out=True,
        )
    )
    daemon._pending_apply_results.clear()
    daemon._intervention_applied_seen.clear()

    # Now a late ack arrives. The handler must not crash; the future is
    # already resolved and not in the pending dict, so nothing to resolve.
    await daemon._handle_intervention_applied(
        {
            "intervention_id": "iv-restart",
            "phase": "apply",
            "success": True,
            "applied_actions": ["a1"],
        }
    )
    # No new pending entries; the original future result stands.
    assert "iv-restart" not in daemon._pending_apply_results
    assert future.result().confirmed is False
    assert future.result().timed_out is True


@pytest.mark.asyncio
async def test_future_resolved_exactly_once():
    """If both the ack and the timeout race, the future must still be
    resolved exactly once (the second resolver short-circuits because
    ``future.done()`` is True)."""
    daemon = _MinimalDaemon()

    resolutions: list[InterventionApplyResult] = []

    async def _double_resolve() -> None:
        # First trigger the ack.
        await daemon._handle_intervention_applied(
            {
                "intervention_id": "iv-once",
                "phase": "apply",
                "success": True,
                "applied_actions": ["a1"],
            }
        )

    asyncio.create_task(_double_resolve())
    result = await daemon.await_apply_confirmation(
        "iv-once", timeout_seconds=0.5,
    )
    resolutions.append(result)
    # Sleep past the timeout to let the watcher fire (and no-op).
    await asyncio.sleep(0.6)

    # If the watcher had double-resolved we'd see two recorder entries for
    # this intervention_id; instead we expect exactly one.
    matched = [
        event_type
        for event_type, payload in daemon.recorder_calls
        if payload.get("intervention_id") == "iv-once"
        and event_type == "intervention_apply_confirmation"
    ]
    assert len(matched) == 1, (
        f"future resolved {len(matched)} times; expected exactly one"
    )
    assert resolutions[0].confirmed is True
    assert resolutions[0].timed_out is False
