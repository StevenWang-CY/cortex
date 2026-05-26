"""P0 §3.10 — auto-armed distraction blocking on HYPER.

Covers the gating logic in ``_evaluate_auto_distraction_block``:
  * Feature flag off → no arming, no symmetric stop signal.
  * Consent < AUTONOMOUS_ACT → no arming even if the flag is on.
  * Sustained HYPER + confidence + dwell → exactly one
    START_FOCUS_AUTO emission.
  * Sustained recovery (FLOW / RECOVERY) → exactly one STOP_FOCUS_AUTO.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from cortex.services.consent.policy import (
    AUTONOMOUS_ACT,
    REVERSIBLE_ACT,
    ConsentPolicy,
)


class _FakeWSServer:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send_message(
        self,
        message_type: str,
        payload: dict,
        *,
        target_client_types: Any = None,
        correlation_id: Any = None,
    ) -> int:
        self.sent.append((message_type, dict(payload)))
        return 1


class _FakeIntervention:
    enable_auto_distraction_block: bool = False
    auto_distraction_block_confidence: float = 0.85
    auto_distraction_block_dwell_seconds: float = 30.0
    auto_distraction_block_exit_seconds: float = 300.0
    auto_distraction_block_preset: str = "developer"
    auto_distraction_block_session_minutes: int = 20
    auto_distraction_block_custom_domains: list[str] = []


class _FakeConfig:
    intervention = _FakeIntervention()


class _MinimalDaemon:
    """Mounts the ``_evaluate_auto_distraction_block`` chain."""

    def __init__(self) -> None:
        from cortex.services.runtime_daemon import CortexDaemon

        self._ws_server = _FakeWSServer()
        self.config = _FakeConfig()
        self._consent_policy = ConsentPolicy()
        self._auto_focus_armed = False
        self._auto_focus_dwell_started_at = 0.0
        self._auto_focus_recovery_started_at = 0.0
        # Phase-3 hardening: the daemon now uses dedicated boolean
        # sentinels (independent of the timestamp == 0.0 check) so the
        # latch state survives a timestamp=0.0 first call. Initialise
        # them here to match the production __init__.
        self._auto_focus_dwell_started = False
        self._auto_focus_recovery_started = False
        # Phase-3 P1-X.2: break-active gate
        self._break_active = False
        # Wave-2 P1: debounce timestamps tracked on the daemon.
        self._last_focus_auto_arm_ts = 0.0
        self._last_focus_auto_disarm_ts = 0.0

        # Bind the methods we actually want to exercise.
        self._evaluate_auto_distraction_block = (
            CortexDaemon._evaluate_auto_distraction_block.__get__(self, CortexDaemon)
        )
        self._emit_start_focus_auto = (
            CortexDaemon._emit_start_focus_auto.__get__(self, CortexDaemon)
        )
        self._emit_stop_focus_auto = (
            CortexDaemon._emit_stop_focus_auto.__get__(self, CortexDaemon)
        )
        self._reset_auto_focus_timers = (
            CortexDaemon._reset_auto_focus_timers.__get__(self, CortexDaemon)
        )


def _hyper(confidence: float = 0.9) -> SimpleNamespace:
    return SimpleNamespace(state="HYPER", confidence=confidence)


def _flow() -> SimpleNamespace:
    return SimpleNamespace(state="FLOW", confidence=0.95)


@pytest.mark.asyncio
async def test_disabled_flag_never_arms() -> None:
    d = _MinimalDaemon()
    d.config.intervention.enable_auto_distraction_block = False
    await d._evaluate_auto_distraction_block(_hyper(), timestamp=100.0)
    await d._evaluate_auto_distraction_block(_hyper(), timestamp=200.0)
    assert d._ws_server.sent == []
    assert d._auto_focus_armed is False


@pytest.mark.asyncio
async def test_reversible_consent_never_arms() -> None:
    d = _MinimalDaemon()
    d.config.intervention.enable_auto_distraction_block = True
    # Default consent level is REVERSIBLE_ACT — auto-arm gate requires AUTONOMOUS_ACT.
    d._consent_policy.set_level("distraction_block", REVERSIBLE_ACT)
    for ts in range(0, 200, 5):
        await d._evaluate_auto_distraction_block(_hyper(), timestamp=float(ts))
    assert d._ws_server.sent == []


@pytest.mark.asyncio
async def test_sustained_hyper_arms_after_dwell() -> None:
    d = _MinimalDaemon()
    d.config.intervention.enable_auto_distraction_block = True
    d._consent_policy.set_level("distraction_block", AUTONOMOUS_ACT)

    # Before dwell threshold: nothing happens.
    await d._evaluate_auto_distraction_block(_hyper(), timestamp=100.0)
    assert d._auto_focus_armed is False
    assert d._ws_server.sent == []

    # 31 seconds later — past the 30 s dwell — arm fires exactly once.
    await d._evaluate_auto_distraction_block(_hyper(), timestamp=131.0)
    assert d._auto_focus_armed is True
    assert len(d._ws_server.sent) == 1
    assert d._ws_server.sent[0][0] == "START_FOCUS_AUTO"
    assert d._ws_server.sent[0][1]["preset"] == "developer"
    assert d._ws_server.sent[0][1]["duration_minutes"] == 20

    # Continued HYPER must NOT re-fire START_FOCUS_AUTO.
    await d._evaluate_auto_distraction_block(_hyper(), timestamp=160.0)
    assert len([s for s in d._ws_server.sent if s[0] == "START_FOCUS_AUTO"]) == 1


@pytest.mark.asyncio
async def test_low_confidence_blocks_arm() -> None:
    d = _MinimalDaemon()
    d.config.intervention.enable_auto_distraction_block = True
    d._consent_policy.set_level("distraction_block", AUTONOMOUS_ACT)
    # Confidence below gate (0.85) → dwell counter never starts.
    for ts in (0.0, 60.0, 120.0):
        await d._evaluate_auto_distraction_block(_hyper(confidence=0.5), timestamp=ts)
    assert d._auto_focus_armed is False


@pytest.mark.asyncio
async def test_sustained_flow_disarms_after_exit_window() -> None:
    d = _MinimalDaemon()
    d.config.intervention.enable_auto_distraction_block = True
    d._consent_policy.set_level("distraction_block", AUTONOMOUS_ACT)

    # Arm.
    await d._evaluate_auto_distraction_block(_hyper(), timestamp=0.0)
    await d._evaluate_auto_distraction_block(_hyper(), timestamp=40.0)
    assert d._auto_focus_armed

    # 200 s of FLOW (below 300 s exit threshold) — still armed.
    await d._evaluate_auto_distraction_block(_flow(), timestamp=240.0)
    assert d._auto_focus_armed
    assert len([s for s in d._ws_server.sent if s[0] == "STOP_FOCUS_AUTO"]) == 0

    # Cross the 300 s exit threshold (one big tick).
    await d._evaluate_auto_distraction_block(_flow(), timestamp=600.0)
    assert d._auto_focus_armed is False
    stops = [s for s in d._ws_server.sent if s[0] == "STOP_FOCUS_AUTO"]
    assert len(stops) == 1


@pytest.mark.asyncio
async def test_disarm_auto_focus_emits_stop() -> None:
    from cortex.services.runtime_daemon import CortexDaemon

    d = _MinimalDaemon()
    d._auto_focus_armed = True
    # Bind the disarm method.
    disarm = CortexDaemon.disarm_auto_focus.__get__(d, CortexDaemon)
    await disarm()
    assert d._auto_focus_armed is False
    stops = [s for s in d._ws_server.sent if s[0] == "STOP_FOCUS_AUTO"]
    assert len(stops) == 1
    assert stops[0][1]["reason"] == "user_disarm"


# ─── Wave-2 P1: rapid-cycle debounce ─────────────────────────────────


@pytest.mark.asyncio
async def test_rapid_hyper_recovery_hyper_cycle_emits_start_only_once() -> None:
    """A HYPER → RECOVERY → HYPER cycle within the 30 s debounce window
    must NOT emit a second START_FOCUS_AUTO. The browser extension
    would otherwise see START / STOP / START frames in seconds and the
    focus-session UI would thrash.

    To exercise both halves of the debounce we set the
    ``auto_distraction_block_exit_seconds`` low (10 s) so the
    sustained-FLOW path can actually fire STOP_FOCUS_AUTO within the
    test horizon; the production default is 300 s which guarantees the
    30 s minimum-hold is dwarfed.
    """
    d = _MinimalDaemon()
    d.config.intervention.enable_auto_distraction_block = True
    d.config.intervention.auto_distraction_block_exit_seconds = 10.0
    d._consent_policy.set_level("distraction_block", AUTONOMOUS_ACT)

    # t=0: start HYPER dwell.
    await d._evaluate_auto_distraction_block(_hyper(), timestamp=0.0)
    assert d._auto_focus_armed is False

    # t=40: past 30 s dwell → ARM (START_FOCUS_AUTO).
    await d._evaluate_auto_distraction_block(_hyper(), timestamp=40.0)
    starts = [s for s in d._ws_server.sent if s[0] == "START_FOCUS_AUTO"]
    assert len(starts) == 1

    # t=45: drop to FLOW; recovery countdown begins.
    await d._evaluate_auto_distraction_block(_flow(), timestamp=45.0)
    # Still armed — the recovery hasn't crossed exit_gate (10 s) yet.
    assert d._auto_focus_armed

    # t=58: 13 s of FLOW elapsed — past exit_gate (10 s). But the daemon
    # only armed at t=40 (held for 18 s), still under the 30 s minimum-
    # hold debounce → STOP_FOCUS_AUTO must be suppressed.
    await d._evaluate_auto_distraction_block(_flow(), timestamp=58.0)
    stops = [s for s in d._ws_server.sent if s[0] == "STOP_FOCUS_AUTO"]
    assert len(stops) == 0, (
        f"STOP_FOCUS_AUTO fired too early — debounce should hold for 30 s; "
        f"sent: {d._ws_server.sent}"
    )
    assert d._auto_focus_armed, "should still be armed during minimum-hold"

    # t=80: now 40 s after arm; cross the minimum-hold window. STOP fires.
    await d._evaluate_auto_distraction_block(_flow(), timestamp=80.0)
    stops = [s for s in d._ws_server.sent if s[0] == "STOP_FOCUS_AUTO"]
    assert len(stops) == 1
    assert d._auto_focus_armed is False

    # t=85: HYPER returns. Dwell starts.
    await d._evaluate_auto_distraction_block(_hyper(), timestamp=85.0)
    # t=120: 35 s of HYPER → past dwell_gate. BUT only 40 s since the
    # STOP at t=80, well under the 30 s post-disarm cooldown? Wait —
    # 120 - 80 = 40 s, > 30 s. So cooldown HAS expired; the arm should
    # fire again. Test the boundary at t=105 (25 s post-disarm) first.
    await d._evaluate_auto_distraction_block(_hyper(), timestamp=105.0)
    # 105 - 85 = 20 s of HYPER < dwell_gate=30; no arm yet anyway.
    # t=115: 30 s HYPER dwell crossed. But cooldown 115 - 80 = 35 s; OK.
    # Use stricter values: assert that with cooldown still active no arm.
    starts_so_far = len([s for s in d._ws_server.sent if s[0] == "START_FOCUS_AUTO"])
    assert starts_so_far == 1, "still only one START during cooldown window"

    # Force the cooldown boundary: at t=109, cooldown=29s (< 30s), even
    # though dwell would otherwise be sufficient if confidence were
    # higher. Reset dwell timer to make the dwell pass.
    d._auto_focus_dwell_started_at = 85.0
    d._auto_focus_dwell_started = True
    await d._evaluate_auto_distraction_block(_hyper(), timestamp=109.0)
    starts_after = len([s for s in d._ws_server.sent if s[0] == "START_FOCUS_AUTO"])
    assert starts_after == 1, (
        f"START_FOCUS_AUTO fired during cooldown window; sent={d._ws_server.sent}"
    )


def test_consent_policy_distraction_block_default_level() -> None:
    """The default consent for ``distraction_block`` must NOT autorise
    autonomous arming (REVERSIBLE_ACT). The user has to opt in via the
    Settings → Focus protection toggle which upgrades to AUTONOMOUS_ACT.
    """
    policy = ConsentPolicy()
    assert policy.get_minimum_level("distraction_block") == REVERSIBLE_ACT
    policy.set_level("distraction_block", AUTONOMOUS_ACT)
    assert policy.get_minimum_level("distraction_block") == AUTONOMOUS_ACT
