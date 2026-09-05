"""Audit defects D6, D9, D13, D16, D17 — intervention engine, fusion, clocks."""

from __future__ import annotations

import importlib.util

import pytest

from cortex.application.clock import FakeClock, monotonic_seconds
from cortex.libs.schemas.features import FeatureName, TelemetryFeatures
from cortex.libs.schemas.intervention import (
    AdapterCommand,
    InterventionPlan,
    UIPlan,
    WorkspaceSnapshot,
)
from cortex.libs.schemas.intervention_transaction import InterventionLifecycleState
from cortex.libs.schemas.leetcode import (
    DestructiveStruggleEstimate,
    LeetCodeContext,
    LeetCodeMode,
    LeetCodeModeEstimate,
    LeetCodeStage,
)
from cortex.libs.schemas.state import SignalQuality, StateEstimate, StateScores
from cortex.services.consent.ladder import ConsentLadder
from cortex.services.consent.policy import ConsentPolicy
from cortex.services.intervention_engine.executor import InterventionExecutor
from cortex.services.intervention_engine.leetcode_interventions import (
    InterventionMatrix,
    RestatementScratchpad,
)
from cortex.services.intervention_engine.restore import (
    MAX_RETAINED_OUTCOMES,
    RESTORE_RETRY_BASE_BACKOFF_SECONDS,
    RestoreManager,
)
from cortex.services.intervention_engine.transaction import (
    InterventionTransactionCoordinator,
    build_action_manifest,
)
from cortex.services.intervention_engine.transaction_store import (
    InMemoryInterventionTransactionStore,
)
from cortex.services.session_report.generator import SessionReportGenerator
from cortex.services.state_engine.feature_fusion import FeatureFusion
from cortex.services.state_engine.rule_scorer import RuleScorer
from cortex.services.state_engine.zombie_detector import ZombieReadingDetector

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _estimate(state: str = "HYPER") -> StateEstimate:
    return StateEstimate(
        state=state,
        confidence=0.9,
        scores=StateScores(flow=0.1, hypo=0.05, hyper=0.9, recovery=0.05),
        reasons=["test"],
        signal_quality=SignalQuality(physio=0.8, kinematics=0.7, telemetry=0.9),
        timestamp=0.0,
        dwell_seconds=10.0,
    )


def _plan(intervention_id: str = "iv-1") -> InterventionPlan:
    return InterventionPlan(
        intervention_id=intervention_id,
        level="overlay_only",
        situation_summary="Test summary.",
        headline="Take one step",
        primary_focus="Test focus",
        micro_steps=["Re-read the failing line"],
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


def _snapshot(intervention_id: str) -> WorkspaceSnapshot:
    return WorkspaceSnapshot(intervention_id=intervention_id, timestamp=0.0)


# ---------------------------------------------------------------------------
# D6 — restore retries back off and outcomes are bounded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_d6_unverified_restore_is_retried_with_backoff_not_every_tick() -> None:
    attempts: list[float] = []

    async def failing_restore(_iid: str, _action: str) -> bool:
        attempts.append(1.0)
        return False

    manager = RestoreManager(timeout_seconds=1.0, restore_callback=failing_restore)
    manager.start_intervention("iv-1", _snapshot("iv-1"), started_at=0.0)
    estimate = _estimate()

    first = await manager.update(estimate, current_time=100.0)
    assert len(first) == 1 and first[0].workspace_restored is False
    # 0.5 s ticks inside the backoff window make no further attempts.
    for tick in (100.5, 101.0, 102.0, 104.0):
        assert await manager.update(estimate, current_time=tick) == []
    assert len(attempts) == 1
    second = await manager.update(estimate, current_time=100.0 + RESTORE_RETRY_BASE_BACKOFF_SECONDS)
    assert len(second) == 1 and len(attempts) == 2
    active = manager.get_active("iv-1")
    assert active is not None and active.restore_attempts == 2
    # Backoff doubles after the second failure.
    assert active.next_restore_attempt_at == pytest.approx(
        100.0 + RESTORE_RETRY_BASE_BACKOFF_SECONDS + 2 * RESTORE_RETRY_BASE_BACKOFF_SECONDS
    )


@pytest.mark.asyncio
async def test_d6_outcome_history_is_bounded() -> None:
    manager = RestoreManager(timeout_seconds=300.0)
    for index in range(MAX_RETAINED_OUTCOMES + 40):
        iid = f"iv-{index}"
        manager.start_intervention(iid, _snapshot(iid), started_at=0.0)
        await manager.dismiss(iid, current_time=1.0)
    assert len(manager.outcomes) == MAX_RETAINED_OUTCOMES
    assert manager.outcomes[-1].intervention_id == f"iv-{MAX_RETAINED_OUTCOMES + 39}"


# ---------------------------------------------------------------------------
# D9 — terminal transactions are archived (bounded)
# ---------------------------------------------------------------------------


def _coordinator(clock: FakeClock, **kwargs: int) -> InterventionTransactionCoordinator:
    return InterventionTransactionCoordinator(
        ConsentLadder(policy=ConsentPolicy(), clock=clock),
        store=InMemoryInterventionTransactionStore(),
        clock=clock,
        **kwargs,
    )


async def _abandoned_proposal(
    coordinator: InterventionTransactionCoordinator, clock: FakeClock, intervention_id: str,
) -> None:
    manifest = build_action_manifest(
        _plan(intervention_id), [], consent_policy=ConsentPolicy(), clock=clock,
    )
    await coordinator.register_proposal(manifest)
    assert await coordinator.abandon(intervention_id, "test") is True


@pytest.mark.asyncio
async def test_d9_terminal_rows_are_archived_after_retention() -> None:
    clock = FakeClock(wall_unix_ms=1_700_000_000_000, mono_ns=1_000)
    coordinator = _coordinator(clock, terminal_retention_days=1)
    await _abandoned_proposal(coordinator, clock, "old")
    clock.advance(wall_ms=2 * 86_400_000, monotonic_ns=1)
    await _abandoned_proposal(coordinator, clock, "fresh")
    assert await coordinator.get_transaction("old") is None
    fresh = await coordinator.get_transaction("fresh")
    assert fresh is not None and fresh.state == InterventionLifecycleState.ABANDONED


@pytest.mark.asyncio
async def test_d9_terminal_rows_are_capped_regardless_of_age() -> None:
    clock = FakeClock(wall_unix_ms=1_700_000_000_000, mono_ns=1_000)
    coordinator = _coordinator(clock, max_terminal_transactions=2)
    for index in range(4):
        await _abandoned_proposal(coordinator, clock, f"iv-{index}")
        clock.advance(wall_ms=1_000, monotonic_ns=1)
    remaining = [
        iid for iid in ("iv-0", "iv-1", "iv-2", "iv-3")
        if await coordinator.get_transaction(iid) is not None
    ]
    assert remaining == ["iv-2", "iv-3"]


# ---------------------------------------------------------------------------
# D13 — measurement provenance and session report transitions
# ---------------------------------------------------------------------------


def test_d13_per_minute_rates_are_stamped_with_their_real_window() -> None:
    fusion = FeatureFusion(clock=FakeClock())
    features = TelemetryFeatures(
        mouse_velocity_mean=100.0,
        mouse_velocity_variance=10.0,
        mouse_jerk_score=0.0,
        click_burst_score=0.0,
        click_frequency=0.1,
        keypress_rate_per_min=10.0,
        keyboard_burst_score=0.0,
        keystroke_interval_variance=100.0,
        backspace_density=0.0,
        correction_rate_per_100_keys=1.0,
        inactivity_seconds=1.0,
        window_switch_rate=4.0,
        scroll_reversal_score=0.0,
        scroll_back_rate_per_min=2.0,
        observation_window_seconds=15.0,
        mouse_move_count=5,
        click_press_count=1,
        key_press_count=3,
        scroll_event_count=1,
        window_focus_event_count=1,
        window_focus_source_available=True,
    )
    fusion.update_telemetry(features, timestamp=1.0)
    vector, _quality = fusion.fuse(timestamp=1.5)
    assert vector.features[FeatureName.TAB_SWITCH_RATE_PER_MIN].source_window_ms == 15_000
    assert vector.features[FeatureName.SCROLL_BACK_RATE_PER_MIN].source_window_ms == 15_000
    assert vector.features[FeatureName.THRASHING_SCORE].source_window_ms == 60_000


def test_d13_session_report_records_transitions_not_ticks() -> None:
    generator = SessionReportGenerator()
    generator.start()
    t = 1_000.0
    for _ in range(100):
        generator.record_state("FLOW", t)
        t += 0.5
    generator.record_state("HYPER", t)
    for _ in range(10):
        t += 0.5
        generator.record_state("HYPER", t)
    report = generator.finish(end_timestamp=t)
    assert len(report.state_transitions) == 1
    assert report.state_transitions[0].from_state == "FLOW"
    assert report.state_transitions[0].to_state == "HYPER"
    assert report.time_in_flow_seconds == pytest.approx(50.0)
    assert report.time_in_hyper_seconds == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# D16 — injected clocks
# ---------------------------------------------------------------------------


def test_d16_zombie_detector_uses_injected_clock() -> None:
    clock = FakeClock(mono_ns=1_000 * 1_000_000_000)
    detector = ZombieReadingDetector(blink_baseline=17.0, min_duration=5.0, cooldown=0.0, clock=clock)
    assert detector.update("HYPO", 10.0, 22.0, "Google Chrome") is False
    clock.advance(monotonic_ns=3 * 1_000_000_000)
    assert detector.accumulation_seconds == pytest.approx(3.0)
    clock.advance(monotonic_ns=3 * 1_000_000_000)
    assert detector.update("HYPO", 10.0, 22.0, "Google Chrome") is True


def test_d16_leetcode_cooldown_uses_injected_clock() -> None:
    clock = FakeClock(mono_ns=10 * 1_000_000_000)
    matrix = InterventionMatrix(clock=clock)
    scratchpad = next(i for i in matrix._interventions if isinstance(i, RestatementScratchpad))
    estimate = LeetCodeModeEstimate(
        mode=LeetCodeMode.DESTRUCTIVE_STRUGGLE,
        stage=LeetCodeStage.READ,
        confidence=0.9,
        aai_score=0.5,
        allostatic_load=100.0,
        destructive=DestructiveStruggleEstimate(
            is_destructive=True, pathway="comprehension", confidence=0.8,
        ),
    )
    context = LeetCodeContext(
        problem_id="42", title="Two Sum", difficulty="Easy", tags=["Array"],
        stage=LeetCodeStage.READ, time_elapsed_s=300.0,
    )
    assert scratchpad.should_trigger(estimate, context) is True
    scratchpad.build_action(estimate, context)
    assert scratchpad.should_trigger(estimate, context) is False
    clock.advance(monotonic_ns=301 * 1_000_000_000)
    assert scratchpad.should_trigger(estimate, context) is True


@pytest.mark.asyncio
async def test_d16_executor_mutation_timestamps_come_from_injected_clock() -> None:
    clock = FakeClock(mono_ns=42 * 1_000_000_000)
    executor = InterventionExecutor(execution_mode="authorized", clock=clock)
    executor._allow_unwired_consent = True

    class _Adapter:
        async def execute(self, action: str, params: dict[str, object]) -> bool:
            return True

    executor.register_adapter("browser", _Adapter())
    mutations = await executor.apply(
        _plan(), [AdapterCommand(adapter="browser", action="show_overlay", params={})],
    )
    assert mutations and mutations[0].timestamp == pytest.approx(monotonic_seconds(clock))


# ---------------------------------------------------------------------------
# D17 — dead code is gone
# ---------------------------------------------------------------------------


def test_d17_deprecated_trigger_and_legacy_scorers_are_removed() -> None:
    import cortex.services.intervention_engine as intervention_engine

    assert not hasattr(intervention_engine, "InterventionTrigger")
    assert importlib.util.find_spec("cortex.services.intervention_engine.trigger") is None
    for name in (
        "_compute_hyper_score",
        "_compute_hypo_score",
        "_compute_flow_score",
        "_compute_recovery_score",
        "score_pulse_elevation",
        "score_hrv_drop",
        "score_blink_suppression",
        "score_posture_collapse",
        "score_workspace_complexity",
    ):
        assert not hasattr(RuleScorer, name), name
    assert hasattr(RuleScorer, "set_tab_categories")
