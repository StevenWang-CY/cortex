"""Deterministic dual-clock and bounded-deadline regression tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from cortex.application.clock import BoundedDeadline, FakeClock
from cortex.libs.config.settings import InterventionConfig
from cortex.libs.schemas.state import SignalQuality, StateEstimate, StateScores
from cortex.libs.schemas.temporal import EventTime
from cortex.services.eval.helpfulness import HelpfulnessTracker
from cortex.services.llm_engine.cost_tracker import CostTracker, _today_iso
from cortex.services.state_engine.feature_fusion import FeatureFusion
from cortex.services.state_engine.smoother import ScoreSmoother
from cortex.services.state_engine.trigger_policy import TriggerPolicy
from cortex.services.telemetry_engine.window_tracker import WindowTracker


def _unix_ms(value: datetime) -> int:
    return int(value.timestamp() * 1_000)


def test_bounded_deadline_ignores_wall_rollback_and_expires_on_monotonic() -> None:
    clock = FakeClock(wall_unix_ms=1_000_000, mono_ns=9_000_000_000)
    deadline = BoundedDeadline.after(clock, 60_000)

    clock.advance(wall_ms=10_000, monotonic_ns=10_000_000_000)
    assert deadline.remaining_ms(clock) == 50_000
    clock.jump_wall(-900_000)
    assert deadline.remaining_ms(clock) == 50_000
    clock.advance(monotonic_ns=50_000_000_000)
    assert deadline.expired(clock)


def test_bounded_deadline_forward_jump_can_expire_but_never_extend() -> None:
    clock = FakeClock(wall_unix_ms=1_000, mono_ns=0)
    deadline = BoundedDeadline.after(clock, 30_000)
    clock.jump_wall(31_000)
    assert deadline.remaining_ms(clock) == 0


def test_bounded_deadline_reboot_uses_wall_remainder_capped_by_duration() -> None:
    clock = FakeClock(wall_unix_ms=1_000_000, mono_ns=8_000_000_000)
    deadline = BoundedDeadline.after(clock, 120_000)
    record = deadline.to_record()

    clock.advance(wall_ms=30_000, monotonic_ns=30_000_000_000)
    clock.reboot(monotonic_ns=0)
    restored = BoundedDeadline.from_record(record)
    assert restored.remaining_ms(clock) == 90_000

    clock.jump_wall(-1_000_000)
    assert restored.remaining_ms(clock) == 120_000


def test_long_host_uptime_keeps_nanosecond_precision() -> None:
    uptime_ns = 400 * 24 * 60 * 60 * 1_000_000_000
    clock = FakeClock(wall_unix_ms=2_000_000_000_000, mono_ns=uptime_ns)
    event = EventTime.from_clock(clock)
    assert event.observed_at_mono_ns == uptime_ns
    assert event.boot_id == clock.boot_id


def test_feature_and_state_pipeline_preserve_one_event_time() -> None:
    clock = FakeClock(wall_unix_ms=1_700_000_000_123, mono_ns=987_654_321)
    event = EventTime.from_clock(clock)
    fusion = FeatureFusion(clock=clock)
    vector, quality = fusion.fuse(event_time=event)
    smoother = ScoreSmoother(clock=clock)
    estimate = smoother.update(
        StateScores(flow=1.0),
        quality,
        event_time=event,
    )

    assert vector.observed_at_unix_ms == event.observed_at_unix_ms
    assert vector.observed_at_mono_ns == event.observed_at_mono_ns
    assert vector.boot_id == event.boot_id
    assert estimate.observed_at_unix_ms == event.observed_at_unix_ms
    assert estimate.observed_at_mono_ns == event.observed_at_mono_ns
    assert estimate.boot_id == event.boot_id


def test_explicit_time_zero_never_falls_back_to_now() -> None:
    clock = FakeClock(wall_unix_ms=10_000, mono_ns=10_000_000_000)
    tracker = WindowTracker(clock=clock)
    tracker.record_focus_event("Editor", timestamp=0.0)
    assert len(tracker.get_events_in_window(1.0, current_time=0.0)) == 1

    helpfulness = HelpfulnessTracker(clock=FakeClock())
    helpfulness.start_tracking("i", "overlay", "FLOW", 0.5)
    helpfulness._clock.advance(monotonic_ns=10_000_000_000)
    helpfulness.record_user_action("i", "dismissed", timestamp=0.0)
    assert helpfulness._active["i"].was_ignored is True


def test_quiet_mode_restart_subtracts_downtime(tmp_path: Path) -> None:
    wall = _unix_ms(datetime(2026, 1, 1, tzinfo=UTC))
    clock = FakeClock(wall_unix_ms=wall, mono_ns=0)
    config = InterventionConfig(
        max_dismissals=3,
        dismissal_window_minutes=5,
        quiet_mode_minutes=15,
    )
    history = tmp_path / "quiet.json"
    dismissal = tmp_path / "dismissal.json"
    policy = TriggerPolicy(
        config,
        clock=clock,
        quiet_mode_history_path=history,
        dismissal_model_path=dismissal,
    )
    for _ in range(3):
        policy.record_dismissal()
        clock.advance(wall_ms=1_000, monotonic_ns=1_000_000_000)
    assert policy.is_quiet_mode
    persisted = json.loads(history.read_text(encoding="utf-8"))
    assert persisted["quiet_mode_deadline"]["expires_at_unix_ms"] > wall

    clock.advance(wall_ms=5 * 60_000, monotonic_ns=5 * 60_000_000_000)
    clock.reboot()
    revived = TriggerPolicy(
        config,
        clock=clock,
        quiet_mode_history_path=history,
        dismissal_model_path=dismissal,
    )
    assert revived.is_quiet_mode
    assert 9 * 60 < revived._quiet_mode_until < 11 * 60


def test_trigger_cooldown_decision_is_independent_of_wall_clock(
    tmp_path: Path,
) -> None:
    clock = FakeClock(wall_unix_ms=1_700_000_000_000, mono_ns=10_000_000_000)
    policy = TriggerPolicy(
        InterventionConfig(cooldown_seconds=60),
        clock=clock,
        quiet_mode_history_path=tmp_path / "quiet.json",
        dismissal_model_path=tmp_path / "dismissal.json",
    )
    estimate = StateEstimate(
        state="HYPER",
        confidence=0.95,
        scores=StateScores(hyper=0.95),
        signal_quality=SignalQuality(physio=1.0, kinematics=1.0, telemetry=1.0),
        timestamp=10.0,
        dwell_seconds=60.0,
    )
    policy.record_intervention()
    clock.advance(wall_ms=5_000, monotonic_ns=5_000_000_000)
    before_jump = policy.evaluate(estimate)
    clock.jump_wall(-1_000_000_000)
    after_rollback = policy.evaluate(estimate)
    clock.jump_wall(2_000_000_000)
    after_forward_jump = policy.evaluate(estimate)

    assert before_jump.reason.startswith("Cooldown active")
    assert after_rollback.cooldown_remaining == before_jump.cooldown_remaining
    assert after_forward_jump.cooldown_remaining == before_jump.cooldown_remaining


def test_cost_ledger_rolls_at_local_midnight_with_fake_clock(tmp_path: Path) -> None:
    local_zone = datetime.now().astimezone().tzinfo
    before_local = datetime(2026, 8, 24, 23, 59, 59, tzinfo=local_zone)
    clock = FakeClock(
        wall_unix_ms=_unix_ms(before_local.astimezone(UTC)),
        mono_ns=0,
    )
    tracker = CostTracker(tmp_path / "cost.json", clock=clock)
    tracker.record("a", "model", 1.0)
    assert tracker.today_total_usd() == pytest.approx(1.0)

    clock.advance(wall_ms=2_000, monotonic_ns=2_000_000_000)
    tracker.record("b", "model", 2.0)
    assert tracker.today_total_usd() == pytest.approx(2.0)
    assert len(tracker._days) == 2
    assert sum(float(day["total_usd"]) for day in tracker._days.values()) == 3.0


def test_dst_transition_maps_instants_to_the_correct_local_date() -> None:
    eastern = ZoneInfo("America/New_York")
    before_fallback = datetime(2026, 11, 1, 1, 30, tzinfo=eastern, fold=0)
    after_fallback = before_fallback + timedelta(hours=1)
    assert _today_iso(before_fallback) == "2026-11-01"
    assert _today_iso(after_fallback) == "2026-11-01"


def test_signal_quality_fixture_is_time_independent() -> None:
    quality = SignalQuality(physio=0.5, kinematics=0.5, telemetry=0.5)
    assert quality.acceptable is True
