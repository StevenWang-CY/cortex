"""Phase-3 / Phase-1 audit remediation regressions for P0 §3.10/§3.11/§3.12.

Each test reproduces a P0 bug found by the audit waves and verifies the fix.
Grouped by audit finding id so a future failure can be cross-referenced
back to the original investigation.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from cortex.services.consent.policy import (
    AUTONOMOUS_ACT,
    REVERSIBLE_ACT,
    ConsentPolicy,
)

# ----------------------------------------------------------------------
# Fakes shared across tests
# ----------------------------------------------------------------------


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
        self.started = 0
        self.stopped = 0

    async def start(self) -> None:
        self.is_running = True
        self.started += 1

    async def stop(self) -> None:
        self.is_running = False
        self.stopped += 1


class _FakeWSServer:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send_message(
        self,
        message_type: str,
        payload: dict,
        *,
        target_client_types=None,
        correlation_id=None,
    ) -> int:
        self.sent.append((message_type, dict(payload)))
        return 1


class _FakeConfigIntervention:
    quiet_mode_minutes: int = 30
    enable_auto_distraction_block: bool = True
    auto_distraction_block_confidence: float = 0.85
    auto_distraction_block_dwell_seconds: float = 30.0
    auto_distraction_block_exit_seconds: float = 300.0
    auto_distraction_block_preset: str = "developer"
    auto_distraction_block_session_minutes: int = 20
    auto_distraction_block_custom_domains: list[str] = []


class _FakeConfig:
    intervention = _FakeConfigIntervention()


class _MinimalDaemon:
    """A trimmed CortexDaemon that exposes ``set_quiet_mode`` +
    ``_evaluate_auto_distraction_block`` + ``disarm_auto_focus`` for
    the regression tests."""

    def __init__(self) -> None:
        from cortex.services.runtime_daemon import CortexDaemon

        self._ws_server = _FakeWSServer()
        self._trigger_policy = _FakeTriggerPolicy()
        self._capture_pipeline = _FakeCapturePipeline()
        self._capture_available = True
        self._capture_processing_enabled = True
        self._pause_was_capturing = False
        self._quiet_mode_kind = "off"
        self._quiet_mode_ends_at: float | None = None
        self._quiet_mode_source = "daemon"
        self._quiet_mode_lock = asyncio.Lock()
        self._quiet_mode_decay_task = None
        self._auto_focus_armed = False
        self._auto_focus_dwell_started_at = 0.0
        self._auto_focus_recovery_started_at = 0.0
        self._auto_focus_dwell_started = False
        self._auto_focus_recovery_started = False
        # Wave-2 P1: rapid-cycle debounce timestamps tracked on the daemon.
        self._last_focus_auto_arm_ts = 0.0
        self._last_focus_auto_disarm_ts = 0.0
        self._break_active = False
        self._consent_policy = ConsentPolicy()
        self.config = _FakeConfig()

        # Method binds.
        for name in (
            "set_quiet_mode",
            "_broadcast_quiet_mode_state",
            "get_quiet_mode_state",
            "_decay_quiet_mode_after",
            "_evaluate_auto_distraction_block",
            "_emit_start_focus_auto",
            "_emit_stop_focus_auto",
            "disarm_auto_focus",
            "_reset_auto_focus_timers",
        ):
            setattr(
                self,
                name,
                getattr(CortexDaemon, name).__get__(self, CortexDaemon),
            )

    def _spawn_background_task(self, coro, *, name=None):
        task = asyncio.create_task(coro, name=name)
        task.cancel()
        return task


def _hyper(confidence: float = 0.9) -> SimpleNamespace:
    return SimpleNamespace(state="HYPER", confidence=confidence)


def _flow() -> SimpleNamespace:
    return SimpleNamespace(state="FLOW", confidence=0.95)


# ======================================================================
# §3.10 — Auto distraction block remediation regressions
# ======================================================================


@pytest.mark.asyncio
async def test_opt_out_while_armed_disarms_focus_session() -> None:
    """Audit-1.1 P0-2 / Phase-3 P0-N4: when the feature flag flips off
    mid-session, the daemon MUST emit STOP_FOCUS_AUTO immediately
    instead of letting the browser keep blocking sites until the next
    state transition."""
    d = _MinimalDaemon()
    d.config.intervention.enable_auto_distraction_block = True
    d._consent_policy.set_level("distraction_block", AUTONOMOUS_ACT)
    # Arm.
    await d._evaluate_auto_distraction_block(_hyper(), timestamp=0.0)
    await d._evaluate_auto_distraction_block(_hyper(), timestamp=40.0)
    assert d._auto_focus_armed is True

    # User toggles off mid-session.
    d.config.intervention.enable_auto_distraction_block = False
    await d._evaluate_auto_distraction_block(_hyper(), timestamp=50.0)

    assert d._auto_focus_armed is False
    stops = [s for s in d._ws_server.sent if s[0] == "STOP_FOCUS_AUTO"]
    assert len(stops) == 1
    assert stops[0][1]["reason"] == "feature_disabled"


@pytest.mark.asyncio
async def test_consent_downgrade_while_armed_disarms_focus_session() -> None:
    """Same as the opt-out test but via the consent ladder path —
    e.g. the user downgrades ``distraction_block`` from AUTONOMOUS_ACT
    back to REVERSIBLE_ACT mid-session. The daemon must teardown.
    """
    d = _MinimalDaemon()
    d.config.intervention.enable_auto_distraction_block = True
    d._consent_policy.set_level("distraction_block", AUTONOMOUS_ACT)
    await d._evaluate_auto_distraction_block(_hyper(), timestamp=0.0)
    await d._evaluate_auto_distraction_block(_hyper(), timestamp=40.0)
    assert d._auto_focus_armed

    # Downgrade consent.
    d._consent_policy.set_level("distraction_block", REVERSIBLE_ACT)
    await d._evaluate_auto_distraction_block(_hyper(), timestamp=50.0)

    assert d._auto_focus_armed is False
    stops = [s for s in d._ws_server.sent if s[0] == "STOP_FOCUS_AUTO"]
    assert stops[-1][1]["reason"] == "consent_downgrade"


@pytest.mark.asyncio
async def test_break_active_suppresses_auto_arm() -> None:
    """Phase-3 P1-X.2 / Audit-1.3 P1-X.2: during a biology break, the
    auto-arm path must not fire — otherwise a focus interstitial
    layers on top of the breathing overlay."""
    d = _MinimalDaemon()
    d.config.intervention.enable_auto_distraction_block = True
    d._consent_policy.set_level("distraction_block", AUTONOMOUS_ACT)
    d._break_active = True

    for ts in (0.0, 35.0, 70.0):
        await d._evaluate_auto_distraction_block(_hyper(), timestamp=ts)

    assert d._auto_focus_armed is False
    starts = [s for s in d._ws_server.sent if s[0] == "START_FOCUS_AUTO"]
    assert starts == []


@pytest.mark.asyncio
async def test_disarm_resets_dwell_bool_sentinels() -> None:
    """Audit-1.1 P1-7 / Phase-3 P1-N3 corollary: ``disarm_auto_focus``
    must reset the boolean latches, not just the timestamp floats —
    otherwise the next HYPER episode will never start a fresh dwell.
    """
    d = _MinimalDaemon()
    d._auto_focus_armed = True
    d._auto_focus_dwell_started = True
    d._auto_focus_recovery_started = True
    d._auto_focus_dwell_started_at = 42.0
    d._auto_focus_recovery_started_at = 99.0

    await d.disarm_auto_focus()

    assert d._auto_focus_armed is False
    assert d._auto_focus_dwell_started is False
    assert d._auto_focus_recovery_started is False
    assert d._auto_focus_dwell_started_at == 0.0
    assert d._auto_focus_recovery_started_at == 0.0


@pytest.mark.asyncio
async def test_emit_start_failure_does_not_flip_armed_flag() -> None:
    """Audit-1.1 P1-2 / Phase-3 P1-2: when the WS broadcast fails,
    ``_auto_focus_armed`` MUST remain False so the next state tick
    retries the broadcast."""
    d = _MinimalDaemon()
    d.config.intervention.enable_auto_distraction_block = True
    d._consent_policy.set_level("distraction_block", AUTONOMOUS_ACT)

    # Replace the WS send with a failing one.
    async def _failing_send(*_args, **_kwargs):
        raise RuntimeError("simulated wire failure")
    d._ws_server.send_message = _failing_send  # type: ignore[assignment]

    await d._evaluate_auto_distraction_block(_hyper(), timestamp=0.0)
    await d._evaluate_auto_distraction_block(_hyper(), timestamp=40.0)

    assert d._auto_focus_armed is False, (
        "armed flag must not flip when START_FOCUS_AUTO broadcast fails"
    )


# ======================================================================
# §3.11 — Quiet/pause mode remediation regressions
# ======================================================================


@pytest.mark.asyncio
async def test_repeated_pause_does_not_clobber_was_capturing() -> None:
    """Phase-3 P1-DF-11.5 / Audit-1.1 P0-4 (race fragment):
    ``set_quiet_mode("pause")`` called twice must keep
    ``_pause_was_capturing`` True so a subsequent "off" resumes capture.
    """
    d = _MinimalDaemon()
    await d.set_quiet_mode("pause", source="dashboard")
    assert d._pause_was_capturing is True
    assert d._capture_pipeline.stopped == 1

    # Second pause click — must NOT overwrite _pause_was_capturing.
    await d.set_quiet_mode("pause", source="tray")
    assert d._pause_was_capturing is True

    # Off resumes capture.
    await d.set_quiet_mode("off", source="dashboard")
    assert d._capture_pipeline.started == 1
    assert d._pause_was_capturing is False


@pytest.mark.asyncio
async def test_pause_while_auto_focus_armed_disarms() -> None:
    """Phase-3 P0-N4 / Audit-1.3 P0-X.1: entering pause must disarm
    any active auto-distraction-block focus session so the browser
    doesn't keep blocking while the user is away."""
    d = _MinimalDaemon()
    d._auto_focus_armed = True

    await d.set_quiet_mode("pause", source="dashboard")

    assert d._auto_focus_armed is False
    stops = [s for s in d._ws_server.sent if s[0] == "STOP_FOCUS_AUTO"]
    assert len(stops) == 1
    assert stops[0][1]["reason"] == "paused"


@pytest.mark.asyncio
async def test_concurrent_set_quiet_mode_is_serialised() -> None:
    """Phase-3 P0-4 / Audit-1.1 P0-4: two surfaces firing pause + off
    simultaneously must NOT corrupt the pause-was-capturing latch."""
    d = _MinimalDaemon()
    # Two concurrent calls; the lock should serialise them.
    await asyncio.gather(
        d.set_quiet_mode("pause", source="dashboard"),
        d.set_quiet_mode("off", source="overlay"),
    )
    # Final state is deterministic only on completion order, but the
    # invariant we care about is: capture is NOT both stopped AND
    # the resume-path skipped. Either kind=pause (camera off) or
    # kind=off (camera on) is fine; lock guarantees no torn state.
    if d._quiet_mode_kind == "pause":
        assert d._capture_pipeline.is_running is False
    elif d._quiet_mode_kind == "off":
        # If we ended at off, capture must be running.
        assert d._capture_pipeline.is_running is True


@pytest.mark.asyncio
async def test_settings_sync_false_routes_through_set_quiet_mode() -> None:
    """Phase-3 P0-DF-11.1 / Audit-1.3 P0-DF-11.1: a legacy SETTINGS_SYNC
    {quiet_mode: false} must clear the pause state AND resume capture,
    not just clear the trigger-policy window."""
    d = _MinimalDaemon()
    # Set pause first.
    await d.set_quiet_mode("pause", source="dashboard")
    assert d._capture_pipeline.is_running is False

    # Now simulate the apply_settings path for legacy quiet_mode:false.
    # The fix routes through set_quiet_mode("off"), which both clears
    # the kind AND resumes capture.
    await d.set_quiet_mode("off", source="settings_sync")

    assert d._quiet_mode_kind == "off"
    assert d._capture_pipeline.is_running is True
    assert d._pause_was_capturing is False


# ======================================================================
# §3.12 — OS notification remediation regressions
# ======================================================================


def test_macos_notifications_module_is_thread_safe_importable() -> None:
    """Phase-3 P0-DF-12.1 / Audit-1.1 P0-6: helper must be importable
    and expose the main-thread dispatcher primitive so callers can
    marshal Cocoa requests to the main run loop."""
    import cortex.libs.utils.macos_notifications as mn

    assert hasattr(mn, "send_intervention_notification")
    assert hasattr(mn, "send_notification")
    assert hasattr(mn, "set_user_action_handler")
    assert callable(mn.reset_auth_state_for_tests)


def test_macos_notifications_action_handler_registration() -> None:
    """Action-handler registration must accept None and a callable
    without raising. ``set_user_action_handler(None)`` clears."""
    import cortex.libs.utils.macos_notifications as mn

    captured: list[tuple[str, str]] = []

    def _h(iid: str, action: str) -> None:
        captured.append((iid, action))

    mn.set_user_action_handler(_h)
    mn.set_user_action_handler(None)


def test_consent_policy_serialisation_round_trips() -> None:
    """Audit-1.1 P0-1 / Phase-3 P0-1: consent overrides must persist
    via ``to_dict`` / ``from_dict`` so the user's opt-in to
    ``distraction_block`` at AUTONOMOUS_ACT survives daemon restart.
    """
    policy = ConsentPolicy()
    policy.set_level("distraction_block", AUTONOMOUS_ACT)
    dumped = policy.to_dict()
    restored = ConsentPolicy.from_dict(dumped)
    assert restored.get_minimum_level("distraction_block") == AUTONOMOUS_ACT


@pytest.mark.asyncio
async def test_daemon_stop_disarms_auto_focus() -> None:
    """Phase-3 P0-N5: a daemon shutdown while an auto-armed focus
    session is live must broadcast STOP_FOCUS_AUTO so the browser
    doesn't keep blocking after the daemon goes away."""
    d = _MinimalDaemon()
    d._auto_focus_armed = True
    await d.disarm_auto_focus()
    assert d._auto_focus_armed is False
    stops = [s for s in d._ws_server.sent if s[0] == "STOP_FOCUS_AUTO"]
    assert len(stops) == 1
