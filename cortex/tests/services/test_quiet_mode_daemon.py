"""P0 §3.11 — daemon-side quiet/pause behaviour (set_quiet_mode).

Exercises the broadcast contract: every kind transition emits a
QUIET_MODE_STATE frame, the trigger policy is updated accordingly,
and pause uniquely releases the capture pipeline while
snooze/quiet leave it running.
"""

from __future__ import annotations

import asyncio
import time

import pytest


class _FakeTriggerPolicy:
    def __init__(self) -> None:
        self.activated_with: list[int] = []
        self.cleared: int = 0

    def activate_quiet_mode(self, duration_minutes: int | None = None) -> None:
        self.activated_with.append(int(duration_minutes or 0))

    def clear_quiet_mode(self) -> None:
        self.cleared += 1


class _FakeCapturePipeline:
    def __init__(self, running: bool = True) -> None:
        self.is_running = running
        self.started: int = 0
        self.stopped: int = 0

    async def start(self) -> None:
        self.is_running = True
        self.started += 1

    async def stop(self) -> None:
        self.is_running = False
        self.stopped += 1


class _FakeWSServer:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict, dict]] = []

    async def send_message(
        self,
        message_type: str,
        payload: dict,
        *,
        target_client_types: list[str] | None = None,
        correlation_id: str | None = None,
    ) -> int:
        self.sent.append(
            (
                message_type,
                dict(payload),
                {"target_client_types": target_client_types, "correlation_id": correlation_id},
            )
        )
        return 1


class _FakeConfigIntervention:
    quiet_mode_minutes: int = 30


class _FakeConfig:
    intervention = _FakeConfigIntervention()


class _MinimalDaemon:
    """Pulls just enough of ``RuntimeDaemon`` to exercise set_quiet_mode."""

    def __init__(self) -> None:
        self._trigger_policy = _FakeTriggerPolicy()
        self._capture_pipeline = _FakeCapturePipeline()
        self._capture_available = True
        self._capture_processing_enabled = True
        self._pause_was_capturing = False
        self._quiet_mode_kind = "off"
        self._quiet_mode_ends_at: float | None = None
        self._quiet_mode_source = "daemon"
        # Phase-3 P0-4: ``set_quiet_mode`` is now lock-protected to
        # serialise concurrent dashboard / tray / overlay calls.
        self._quiet_mode_lock = asyncio.Lock()
        # Auto-decay task ref; tests drop their reference immediately
        # by stubbing _spawn_background_task to a no-op.
        self._quiet_mode_decay_task = None
        # No auto-focus session armed in these tests; the pause-branch
        # disarm path early-exits when the flag is False.
        self._auto_focus_armed = False
        # Latches used by the auto-arm timer; pause path resets them.
        self._auto_focus_dwell_started_at = 0.0
        self._auto_focus_recovery_started_at = 0.0
        self._auto_focus_dwell_started = False
        self._auto_focus_recovery_started = False
        self._ws_server = _FakeWSServer()
        self.config = _FakeConfig()

    def _spawn_background_task(self, coro, *, name=None):
        """Test stub — schedule and immediately cancel so the test's
        event loop doesn't wait for the auto-decay timer."""
        task = asyncio.create_task(coro, name=name)
        # Drop the reference; the production daemon tracks via a set.
        task.cancel()
        return task

    # Pull the methods directly from RuntimeDaemon to avoid duplicating
    # the implementation in the test.
    from cortex.services.runtime_daemon import CortexDaemon

    get_quiet_mode_state = CortexDaemon.get_quiet_mode_state
    _broadcast_quiet_mode_state = CortexDaemon._broadcast_quiet_mode_state
    set_quiet_mode = CortexDaemon.set_quiet_mode
    _decay_quiet_mode_after = CortexDaemon._decay_quiet_mode_after
    _emit_stop_focus_auto = CortexDaemon._emit_stop_focus_auto
    _reset_auto_focus_timers = CortexDaemon._reset_auto_focus_timers


@pytest.mark.asyncio
async def test_set_quiet_mode_snooze_15_does_not_release_camera() -> None:
    d = _MinimalDaemon()
    await d.set_quiet_mode("snooze_15", duration_minutes=15, source="overlay")
    assert d._quiet_mode_kind == "snooze_15"
    assert d._trigger_policy.activated_with == [15]
    # Snooze must NOT touch the camera.
    assert d._capture_pipeline.stopped == 0
    # Both QUIET_MODE_STATE and the legacy SETTINGS_SYNC are emitted.
    types = [s[0] for s in d._ws_server.sent]
    assert "QUIET_MODE_STATE" in types
    assert "SETTINGS_SYNC" in types
    # The QUIET_MODE_STATE payload carries the kind + ends_at.
    state_frame = next(s for s in d._ws_server.sent if s[0] == "QUIET_MODE_STATE")
    assert state_frame[1]["kind"] == "snooze_15"
    assert state_frame[1]["source"] == "overlay"
    assert state_frame[1]["ends_at"] is not None


@pytest.mark.asyncio
async def test_set_quiet_mode_pause_releases_camera() -> None:
    d = _MinimalDaemon()
    await d.set_quiet_mode("pause", source="tray")
    assert d._quiet_mode_kind == "pause"
    # Pause releases the camera handle.
    assert d._capture_pipeline.stopped == 1
    assert d._pause_was_capturing is True
    # Pause uses a long quiet window so trigger policy still suppresses.
    assert d._trigger_policy.activated_with == [240]


@pytest.mark.asyncio
async def test_set_quiet_mode_off_resumes_capture_after_pause() -> None:
    d = _MinimalDaemon()
    await d.set_quiet_mode("pause", source="tray")
    assert d._capture_pipeline.stopped == 1
    await d.set_quiet_mode("off", source="tray")
    # Resume restarts capture.
    assert d._capture_pipeline.started == 1
    assert d._quiet_mode_kind == "off"
    # Quiet window is cleared.
    assert d._trigger_policy.cleared == 1


@pytest.mark.asyncio
async def test_set_quiet_mode_quiet_session_uses_default_duration() -> None:
    d = _MinimalDaemon()
    await d.set_quiet_mode("quiet_session")
    # Default from ``InterventionConfig.quiet_mode_minutes`` (30 in our stub).
    assert d._trigger_policy.activated_with == [30]
    assert d._quiet_mode_kind == "quiet_session"


@pytest.mark.asyncio
async def test_get_quiet_mode_state_decays_after_window() -> None:
    d = _MinimalDaemon()
    d._quiet_mode_kind = "snooze_15"
    d._quiet_mode_ends_at = time.time() - 1  # already expired
    state = d.get_quiet_mode_state()
    assert state["kind"] == "off"
    assert state["ends_at"] is None
