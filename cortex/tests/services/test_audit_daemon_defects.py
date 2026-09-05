"""Audit defects D5, D6, D7, D10, D11, D12, D14, D15 — runtime daemon.

These tests construct a real ``CortexDaemon`` (hardware-free: nothing is
started) and drive the individual proposal / bookkeeping paths directly.
Receptivity probes are stubbed so the shared interruption gate is
deterministic on every machine.
"""

from __future__ import annotations

import asyncio
import stat
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

import cortex.services.runtime_daemon as daemon_module
from cortex.application.clock import FakeClock
from cortex.libs.schemas.context import TaskContext
from cortex.libs.schemas.intervention import InterventionPlan, UIPlan, WorkspaceSnapshot
from cortex.libs.schemas.leetcode import LeetCodeContext, LeetCodeStage
from cortex.libs.schemas.state import SignalQuality, StateEstimate, StateScores
from cortex.services.intervention_engine.transaction import (
    InterventionTransactionCoordinator,
)
from cortex.services.intervention_engine.transaction_store import (
    InMemoryInterventionTransactionStore,
)
from cortex.services.runtime_daemon import CortexDaemon, SessionRecorder
from cortex.services.state_engine.trigger_policy import TriggerDecision

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class _WSStub:
    authenticated_client_count = 1

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def send_message(self, message_type: str, payload: dict[str, Any], **_kw: Any) -> int:
        self.sent.append((message_type, payload))
        return 1

    async def send_intervention(self, plan: InterventionPlan, **_kw: Any) -> int:
        self.sent.append(("INTERVENTION_TRIGGER", plan.model_dump(mode="json")))
        return 1

    async def send_restore(self, intervention_id: str, *, user_action: str) -> int:
        self.sent.append(("RESTORE", {"intervention_id": intervention_id, "user_action": user_action}))
        return 1

    async def send_restore_command(self, _command: Any) -> int:
        return 0

    def connected_client_types(self) -> list[str]:
        return []


class _LLMStub:
    def __init__(self, plan: InterventionPlan | None = None, delay: float = 0.0) -> None:
        self.plan = plan or _plan()
        self.delay = delay
        self.calls = 0

    async def generate_intervention_plan(
        self, _context: Any, _estimate: Any, template_name: str | None = None,
    ) -> InterventionPlan:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.plan


def _plan(intervention_id: str = "iv-special") -> InterventionPlan:
    return InterventionPlan(
        intervention_id=intervention_id,
        level="overlay_only",
        situation_summary="Reading without absorbing.",
        headline="Quick recall check",
        primary_focus="Recall the last paragraph",
        micro_steps=["Say the key idea out loud"],
        hide_targets=[],
        ui_plan=UIPlan(
            dim_background=False,
            show_overlay=True,
            fold_unrelated_code=False,
            intervention_type="overlay_only",
        ),
        tone="direct",
        suggested_actions=[],
    )


def _context() -> TaskContext:
    return TaskContext(mode="reading_docs", active_app="chrome", complexity_score=0.8)


def _estimate(
    state: str = "HYPO",
    *,
    physio: float = 0.9,
    kinematics: float = 0.9,
    telemetry: float = 0.9,
) -> StateEstimate:
    return StateEstimate(
        state=state,
        confidence=0.9,
        scores=StateScores(flow=0.05, hypo=0.85, hyper=0.05, recovery=0.0),
        reasons=["test"],
        signal_quality=SignalQuality(physio=physio, kinematics=kinematics, telemetry=telemetry),
        timestamp=0.0,
        dwell_seconds=120.0,
        evidence_coverage=0.8,
    )


def _decision(*, should_trigger: bool, receptivity_blocked: bool = False) -> TriggerDecision:
    return TriggerDecision(
        should_trigger=should_trigger,
        reason="test",
        confidence=0.9,
        cooldown_remaining=0.0,
        quiet_mode_active=False,
        effective_threshold=0.7,
        receptivity_blocked=receptivity_blocked,
    )


@pytest.fixture
def daemon(monkeypatch: pytest.MonkeyPatch) -> CortexDaemon:
    monkeypatch.setattr(daemon_module.receptivity, "is_microphone_in_use", lambda: False)
    monkeypatch.setattr(daemon_module.receptivity, "is_app_fullscreen", lambda: False)
    instance = CortexDaemon()
    # ``get_config()`` is process-wide; work on a private copy so per-test
    # tuning (cap, cooldown, work hours) cannot leak into other tests.
    instance.config = instance.config.model_copy(deep=True)
    instance.config.intervention.receptivity_work_hours_start = 0
    instance.config.intervention.receptivity_work_hours_end = 24
    instance._trigger_policy.update_thresholds(
        instance.config.intervention, state_config=instance.config.state,
    )
    instance._transaction_coordinator = InterventionTransactionCoordinator(
        instance._consent_ladder,
        store=InMemoryInterventionTransactionStore(),
        clock=instance._clock,
    )
    instance._ws_server = _WSStub()  # type: ignore[assignment]
    return instance


# ---------------------------------------------------------------------------
# D5 — special surfaces pass the shared interruption gate
# ---------------------------------------------------------------------------


async def _run_special(daemon: CortexDaemon, llm: _LLMStub) -> None:
    daemon._llm_client = llm  # type: ignore[assignment]
    await daemon._trigger_special_intervention(
        _context(),
        _estimate(),
        template_name="active_recall",
        ws_type="ACTIVE_RECALL",
        current_time=1_000.0,
    )


@pytest.mark.asyncio
async def test_d5_special_intervention_blocked_by_quiet_mode(daemon: CortexDaemon) -> None:
    daemon._trigger_policy.activate_quiet_mode(duration_minutes=15, current_time=1_000.0)
    llm = _LLMStub()
    await _run_special(daemon, llm)
    assert llm.calls == 0
    assert daemon._active_intervention_id is None
    assert daemon._trigger_policy.intervention_count == 0


@pytest.mark.asyncio
async def test_d5_special_intervention_blocked_when_interventions_disabled(daemon: CortexDaemon) -> None:
    daemon._interventions_enabled = False
    llm = _LLMStub()
    await _run_special(daemon, llm)
    assert llm.calls == 0 and daemon._active_intervention_id is None


@pytest.mark.asyncio
async def test_d5_special_intervention_blocked_by_active_microphone(
    daemon: CortexDaemon, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(daemon_module.receptivity, "is_microphone_in_use", lambda: True)
    llm = _LLMStub()
    await _run_special(daemon, llm)
    assert llm.calls == 0 and daemon._active_intervention_id is None


@pytest.mark.asyncio
async def test_d5_special_intervention_blocked_by_hourly_cap(daemon: CortexDaemon) -> None:
    daemon.config.intervention.cooldown_seconds = 0
    daemon.config.intervention.max_interventions_per_hour = 1
    daemon._trigger_policy.record_intervention(timestamp=990.0)
    llm = _LLMStub()
    await _run_special(daemon, llm)
    assert llm.calls == 0 and daemon._active_intervention_id is None


@pytest.mark.asyncio
async def test_d5_special_intervention_records_itself_on_the_policy(daemon: CortexDaemon) -> None:
    llm = _LLMStub()
    await _run_special(daemon, llm)
    assert llm.calls == 1
    assert daemon._active_intervention_id == llm.plan.intervention_id
    assert daemon._trigger_policy.intervention_count == 1
    ws = daemon._ws_server
    assert isinstance(ws, _WSStub) and ws.sent and ws.sent[-1][0] == "ACTIVE_RECALL"
    # The recorded intervention now holds the cooldown for the next surface.
    gate = daemon._trigger_policy.check_interruption_gate(current_time=1_001.0)
    assert gate.allowed is False and "Cooldown" in gate.reason


@pytest.mark.asyncio
async def test_d5_leetcode_actions_pass_the_shared_gate(
    daemon: CortexDaemon, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executed: list[str] = []

    class _Adapter:
        context = LeetCodeContext(
            problem_id="42", title="Two Sum", difficulty="Easy", tags=["Array"],
            stage=LeetCodeStage.READ, time_elapsed_s=300.0,
        )

        async def health_check(self) -> bool:
            return True

        async def execute(self, action: str, _params: dict[str, Any]) -> Any:
            executed.append(action)
            return SimpleNamespace(success=True, error=None)

    daemon._leetcode_adapter = _Adapter()  # type: ignore[assignment]
    monkeypatch.setattr(
        daemon._leetcode_interventions,
        "select",
        lambda *_a, **_k: [
            {"action": "show_scratchpad", "required_consent_level": "preview", "payload": {"x": 1}}
        ],
    )

    async def allow(**_kw: Any) -> Any:
        return SimpleNamespace(allowed=True, reason="ok")

    monkeypatch.setattr(daemon._consent_ladder, "check", allow)
    monkeypatch.setattr(CortexDaemon, "workspace_mutation_allowed", property(lambda self: True))
    estimate = _estimate("HYPER")
    vector = SimpleNamespace(hrv_rmssd=None)

    daemon._trigger_policy.activate_quiet_mode(duration_minutes=15, current_time=1_000.0)
    await daemon._maybe_trigger_leetcode_interventions(estimate, vector, 1_000.0)
    assert executed == []

    daemon._trigger_policy.clear_quiet_mode()
    await daemon._maybe_trigger_leetcode_interventions(estimate, vector, 1_001.0)
    assert executed == ["show_scratchpad"]
    assert daemon._trigger_policy.intervention_count == 1


# ---------------------------------------------------------------------------
# D6 — special planner call is bounded; restores run off the state loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_d6_special_planner_call_is_bounded_by_wait_for(
    daemon: CortexDaemon, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(daemon, "_planner_wait_timeout_seconds", lambda: 0.05)
    llm = _LLMStub(delay=5.0)
    started = time.monotonic()
    await _run_special(daemon, llm)
    assert time.monotonic() - started < 2.0
    assert llm.calls == 1
    assert daemon._active_intervention_id is None


def test_d6_planner_wait_uses_worst_case_bound(daemon: CortexDaemon) -> None:
    daemon._llm_client = SimpleNamespace(worst_case_seconds=12.5)  # type: ignore[assignment]
    assert daemon._planner_wait_timeout_seconds() == pytest.approx(17.5)
    daemon._llm_client = SimpleNamespace()  # type: ignore[assignment]
    expected = float(daemon.config.llm.planner_worst_case_seconds) + 5.0
    assert daemon._planner_wait_timeout_seconds() == pytest.approx(expected)


@pytest.mark.asyncio
async def test_d6_restore_updates_run_off_the_state_loop_one_at_a_time(
    daemon: CortexDaemon, monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon._restore_manager.start_intervention(
        "iv-r", WorkspaceSnapshot(intervention_id="iv-r", timestamp=0.0), started_at=0.0,
    )
    release = asyncio.Event()
    calls: list[float] = []

    async def fake_updates(_estimate: Any, timestamp: float) -> None:
        calls.append(timestamp)
        await release.wait()

    monkeypatch.setattr(daemon, "_handle_restore_updates", fake_updates)
    estimate = _estimate()
    daemon._dispatch_restore_updates(estimate, 1.0)
    await asyncio.sleep(0)
    daemon._dispatch_restore_updates(estimate, 2.0)  # a pass is running: no overlap
    await asyncio.sleep(0)
    assert calls == [1.0]
    release.set()
    await asyncio.sleep(0.01)
    daemon._dispatch_restore_updates(estimate, 3.0)
    await asyncio.sleep(0)
    assert calls == [1.0, 3.0]


# ---------------------------------------------------------------------------
# D7 / D10 / D11 — policy decision point
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_d7_failing_policy_decision_advances_the_gate_instead_of_spinning(
    daemon: CortexDaemon, monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    async def failing_decide(_request: Any) -> Any:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("repository unavailable")

    monkeypatch.setattr(daemon._policy_lifecycle, "decide", failing_decide)
    assert daemon._next_policy_decision_mono_ns == 0
    spawned = await daemon._maybe_propose_policy_intervention(
        _context(), _estimate("HYPER"), _decision(should_trigger=True), 1_000.0,
    )
    assert spawned is False
    assert daemon._active_intervention_id is None
    assert daemon._next_policy_decision_mono_ns > daemon._clock.monotonic_ns()
    # Inside the decision interval the failing repository is not hammered.
    for _ in range(5):
        await daemon._maybe_propose_policy_intervention(
            _context(), _estimate("HYPER"), _decision(should_trigger=True), 1_001.0,
        )
    assert attempts == 1


@pytest.mark.asyncio
async def test_d10_receptivity_block_records_an_unavailable_decision_point(
    daemon: CortexDaemon, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Any] = []

    async def decide(request: Any) -> Any:
        seen.append(request)
        return SimpleNamespace(selected_arm="no_action", decision_id=uuid4(), propensities=None)

    monkeypatch.setattr(daemon._policy_lifecycle, "decide", decide)
    monkeypatch.setattr(daemon._trigger_policy, "hyper_eligible", lambda _e, current_time=None: True)
    spawned = await daemon._maybe_propose_policy_intervention(
        _context(),
        _estimate("HYPER"),
        _decision(should_trigger=False, receptivity_blocked=True),
        1_000.0,
    )
    assert spawned is False
    assert len(seen) == 1
    assert seen[0].eligible is True
    assert seen[0].available is False
    assert seen[0].availability_reason == "receptivity_blocked"
    assert daemon._active_intervention_id is None


@pytest.mark.asyncio
async def test_d10_receptivity_block_of_ineligible_estimate_is_not_a_decision_point(
    daemon: CortexDaemon, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Any] = []

    async def decide(request: Any) -> Any:
        seen.append(request)
        return SimpleNamespace(selected_arm="no_action", decision_id=uuid4(), propensities=None)

    monkeypatch.setattr(daemon._policy_lifecycle, "decide", decide)
    monkeypatch.setattr(daemon._trigger_policy, "hyper_eligible", lambda _e, current_time=None: False)
    await daemon._maybe_propose_policy_intervention(
        _context(), _estimate("HYPER"), _decision(should_trigger=False, receptivity_blocked=True), 1.0,
    )
    assert seen == []


@pytest.mark.asyncio
async def test_d11_camera_off_estimate_can_still_be_proposed(
    daemon: CortexDaemon, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def decide(_request: Any) -> Any:
        return SimpleNamespace(selected_arm="suggest_only", decision_id=uuid4(), propensities=None)

    captured: list[str | None] = []

    async def fake_trigger(
        _context: Any, _estimate: Any, *, template_name: str | None = None, policy_decision: Any = None,
    ) -> None:
        captured.append(template_name)

    monkeypatch.setattr(daemon._policy_lifecycle, "decide", decide)
    monkeypatch.setattr(daemon, "_trigger_intervention", fake_trigger)
    camera_off = _estimate("HYPER", physio=0.0, kinematics=0.0, telemetry=0.9)
    assert camera_off.signal_quality.acceptable is False
    spawned = await daemon._maybe_propose_policy_intervention(
        _context(), camera_off, _decision(should_trigger=True), 1_000.0,
    )
    assert spawned is True
    assert daemon._active_intervention_id == "__pending__"
    await asyncio.sleep(0.01)
    assert captured == [daemon._policy_arm_to_template("suggest_only")]


# ---------------------------------------------------------------------------
# D12 — helpfulness tracking closes on automatic recovery; nightly diagnostics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_d12_automatic_close_ends_helpfulness_tracking(daemon: CortexDaemon) -> None:
    daemon._helpfulness.start_tracking(
        intervention_id="iv-h", intervention_type="overlay_only", state="HYPER", confidence=0.9,
    )

    async def verified_restore(_iid: str, _action: str) -> bool:
        return True

    daemon._restore_manager.set_restore_callback(verified_restore)
    daemon._restore_manager.start_intervention(
        "iv-h", WorkspaceSnapshot(intervention_id="iv-h", timestamp=0.0), started_at=0.0,
    )
    daemon._active_intervention_id = "iv-h"
    await daemon._handle_restore_updates(_estimate("FLOW"), 10_000.0)  # timed out
    assert "iv-h" not in daemon._helpfulness._active
    assert daemon._active_intervention_id is None
    ws = daemon._ws_server
    assert isinstance(ws, _WSStub)
    assert ("RESTORE", {"intervention_id": "iv-h", "user_action": "timed_out"}) in ws.sent


@pytest.mark.asyncio
async def test_d12_nightly_diagnostics_cover_the_completed_local_day(
    daemon: CortexDaemon, monkeypatch: pytest.MonkeyPatch,
) -> None:
    hour = daemon.config.eval.policy_diagnostics.nightly_hour_local
    local_now = datetime(2026, 9, 4, hour, 1, 0).astimezone()
    daemon._clock = FakeClock(wall_unix_ms=int(local_now.timestamp() * 1000), mono_ns=1)
    calls: list[tuple[Any, Any]] = []

    async def fake_generate(_repository: Any, _root: Any, *, day: str | None = None, tz: Any = None) -> Path:
        calls.append((day, tz))
        return Path("/dev/null")

    monkeypatch.setattr(daemon_module, "generate_daily_policy_diagnostics", fake_generate)
    assert await daemon._generate_policy_diagnostics_if_due() is True
    assert calls[0][0] == "2026-09-03"
    assert calls[0][1] is not None
    assert calls[0][1].utcoffset(local_now) == local_now.utcoffset()
    # Same completed day is never generated twice.
    assert await daemon._generate_policy_diagnostics_if_due() is False
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# D14 — shutdown tolerates listeners that never started
# ---------------------------------------------------------------------------


def test_d14_failing_listener_stop_does_not_abort_teardown(daemon: CortexDaemon) -> None:
    stopped: list[str] = []

    class _Raising:
        def stop(self) -> None:
            raise RuntimeError("listener never started")

    class _Tracker:
        def stop(self) -> None:
            stopped.append("window_tracker")

    daemon._input_hooks = _Raising()  # type: ignore[assignment]
    daemon._window_tracker = _Tracker()  # type: ignore[assignment]
    daemon._stop_input_listeners()
    assert stopped == ["window_tracker"]


# ---------------------------------------------------------------------------
# D15 — session JSONL is owner-only, lazy, and transition-only
# ---------------------------------------------------------------------------


def test_d15_session_file_is_created_lazily_with_owner_only_permissions(tmp_path: Path) -> None:
    recorder = SessionRecorder(str(tmp_path))
    try:
        assert not recorder._path.exists()
        recorder.append("evt", {"i": 1})
    finally:
        recorder.flush(timeout=5.0)
    assert recorder._path.exists()
    assert stat.S_IMODE(recorder._path.stat().st_mode) == 0o600


def test_d15_state_stream_records_transitions_only() -> None:
    class _Recorder:
        def __init__(self) -> None:
            self.records: list[str] = []

        def append(self, event_type: str, _payload: dict[str, Any]) -> None:
            self.records.append(event_type)

    host = SimpleNamespace(_recorder=_Recorder(), _last_recorded_state_key=None, _record_full_state_stream=False)
    flow = _estimate("FLOW")
    for _ in range(50):
        CortexDaemon._record_state_estimate(host, flow)  # type: ignore[arg-type]
    CortexDaemon._record_state_estimate(host, _estimate("HYPER"))  # type: ignore[arg-type]
    assert host._recorder.records == ["state_estimate", "state_estimate"]
    host._record_full_state_stream = True
    CortexDaemon._record_state_estimate(host, _estimate("HYPER"))  # type: ignore[arg-type]
    assert len(host._recorder.records) == 3
