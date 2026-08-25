"""WP6 containment for legacy auto-armed distraction blocking.

Covers the gating logic in ``_evaluate_auto_distraction_block``:
  * Feature flag off → no arming, no symmetric stop signal.
  * Consent < AUTONOMOUS_ACT → no arming even if the flag is on.
  * Sustained HYPER + confidence + dwell still cannot mint authority.
  * STOP remains available only to clean up explicitly-owned legacy state.
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
    execution_mode: str = "authorized"
    enable_auto_distraction_block: bool = False
    auto_distraction_block_confidence: float = 0.85
    auto_distraction_block_dwell_seconds: float = 30.0
    auto_distraction_block_exit_seconds: float = 300.0
    auto_distraction_block_preset: str = "developer"
    auto_distraction_block_custom_domains: list[str] = []


class _FakeConfig:
    def __init__(self) -> None:
        self.intervention = _FakeIntervention()


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

    @property
    def workspace_mutation_allowed(self) -> bool:
        return self.config.intervention.execution_mode != "suggest_only"

    @property
    def intervention_execution_mode(self) -> str:
        return self.config.intervention.execution_mode


def _hyper(confidence: float = 0.9) -> SimpleNamespace:
    return SimpleNamespace(
        state="HYPER",
        support_state="support_likely",
        status="estimated",
        confidence=confidence,
        evidence_coverage=1.0,
    )


def _flow() -> SimpleNamespace:
    return SimpleNamespace(
        state="FLOW",
        support_state="flow_like",
        status="estimated",
        confidence=0.95,
        evidence_coverage=1.0,
    )


@pytest.mark.asyncio
async def test_disabled_flag_never_arms() -> None:
    d = _MinimalDaemon()
    d.config.intervention.enable_auto_distraction_block = False
    await d._evaluate_auto_distraction_block(_hyper(), timestamp=100.0)
    await d._evaluate_auto_distraction_block(_hyper(), timestamp=200.0)
    assert d._ws_server.sent == []
    assert d._auto_focus_armed is False


@pytest.mark.asyncio
async def test_suggest_only_mode_never_arms() -> None:
    d = _MinimalDaemon()
    d.config.intervention.execution_mode = "suggest_only"
    d.config.intervention.enable_auto_distraction_block = True
    d._consent_policy.set_level("distraction_block", AUTONOMOUS_ACT)

    for ts in (0.0, 40.0, 80.0):
        await d._evaluate_auto_distraction_block(_hyper(), timestamp=ts)

    assert d._auto_focus_armed is False
    assert d._ws_server.sent == []


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
async def test_sustained_hyper_never_arms_via_legacy_command() -> None:
    d = _MinimalDaemon()
    d.config.intervention.enable_auto_distraction_block = True
    d._consent_policy.set_level("distraction_block", AUTONOMOUS_ACT)

    # Before dwell threshold: nothing happens.
    await d._evaluate_auto_distraction_block(_hyper(), timestamp=100.0)
    assert d._auto_focus_armed is False
    assert d._ws_server.sent == []

    # Even after the policy dwell, the legacy channel is not authority.
    await d._evaluate_auto_distraction_block(_hyper(), timestamp=131.0)
    assert d._auto_focus_armed is False
    assert d._ws_server.sent == []

    # Continued HYPER cannot turn the state estimate into a capability.
    await d._evaluate_auto_distraction_block(_hyper(), timestamp=160.0)
    assert d._auto_focus_armed is False
    assert d._ws_server.sent == []


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
async def test_flow_after_contained_hyper_has_no_synthetic_stop() -> None:
    d = _MinimalDaemon()
    d.config.intervention.enable_auto_distraction_block = True
    d._consent_policy.set_level("distraction_block", AUTONOMOUS_ACT)

    await d._evaluate_auto_distraction_block(_hyper(), timestamp=0.0)
    await d._evaluate_auto_distraction_block(_hyper(), timestamp=40.0)
    assert d._auto_focus_armed is False

    await d._evaluate_auto_distraction_block(_flow(), timestamp=240.0)
    await d._evaluate_auto_distraction_block(_flow(), timestamp=600.0)
    assert d._auto_focus_armed is False
    assert d._ws_server.sent == []


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
async def test_rapid_hyper_recovery_cycle_remains_contained() -> None:
    """No state-cycle timing can bypass exact transaction authority."""
    d = _MinimalDaemon()
    d.config.intervention.enable_auto_distraction_block = True
    d.config.intervention.auto_distraction_block_exit_seconds = 10.0
    d._consent_policy.set_level("distraction_block", AUTONOMOUS_ACT)

    for estimate, timestamp in (
        (_hyper(), 0.0),
        (_hyper(), 40.0),
        (_flow(), 45.0),
        (_flow(), 80.0),
        (_hyper(), 85.0),
        (_hyper(), 120.0),
    ):
        await d._evaluate_auto_distraction_block(estimate, timestamp=timestamp)
    assert d._auto_focus_armed is False
    assert d._ws_server.sent == []


def test_consent_policy_distraction_block_default_level() -> None:
    """The default consent for ``distraction_block`` must NOT autorise
    autonomous arming (REVERSIBLE_ACT). The user has to opt in via the
    Settings → Focus protection toggle which upgrades to AUTONOMOUS_ACT.
    """
    policy = ConsentPolicy()
    assert policy.get_minimum_level("distraction_block") == REVERSIBLE_ACT
    policy.set_level("distraction_block", AUTONOMOUS_ACT)
    assert policy.get_minimum_level("distraction_block") == AUTONOMOUS_ACT
