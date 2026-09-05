"""Audit defects D1-D5, D8, D10, D11 — state engine / trigger policy.

Every test here is hardware-free and drives the policy with synthetic
timestamps. Each test names the defect it pins so a regression is
attributable.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from cortex.application.clock import FakeClock
from cortex.libs.config.settings import InterventionConfig, StateConfig
from cortex.libs.schemas.features import FeatureName, FeatureValue, FeatureVector
from cortex.libs.schemas.observations import MissingReason
from cortex.libs.schemas.state import (
    SignalQuality,
    StateEstimate,
    StateScores,
    UserBaselines,
)
from cortex.services.state_engine.feature_schema import FEATURE_DEFINITIONS
from cortex.services.state_engine.rule_scorer import RuleScorer
from cortex.services.state_engine.smoother import (
    EXIT_TO_UNKNOWN_DWELL_SECONDS,
    ScoreSmoother,
)
from cortex.services.state_engine.trigger_policy import (
    DISMISSAL_MODEL_MIN_OUTCOMES,
    InterruptionGateDecision,
    TriggerPolicy,
)
from cortex.services.telemetry_engine.feature_aggregator import FeatureAggregator
from cortex.services.telemetry_engine.input_hooks import InputHooks, KeyType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid(name: FeatureName, value: float) -> FeatureValue:
    definition = FEATURE_DEFINITIONS[name]
    return FeatureValue(
        value=value,
        valid=True,
        quality=1.0,
        age_ms=0,
        source_window_ms=definition.source_window_ms,
        algorithm_version="test-v1",
    )


def _missing(name: FeatureName) -> FeatureValue:
    definition = FEATURE_DEFINITIONS[name]
    return FeatureValue(
        valid=False,
        quality=0.0,
        age_ms=0,
        source_window_ms=definition.source_window_ms,
        algorithm_version="test-v1",
        missing_reason=MissingReason.SOURCE_DISCONNECTED,
    )


def _vector(values: dict[FeatureName, float]) -> FeatureVector:
    return FeatureVector(
        timestamp=1.0,
        telemetry_seen_count=5,
        features={
            name: _valid(name, values[name]) if name in values else _missing(name)
            for name in FeatureName
        },
    )


def _hyper(
    *,
    confidence: float,
    dwell: float,
    physio: float = 0.9,
    kinematics: float = 0.9,
    telemetry: float = 0.9,
    coverage: float = 1.0,
) -> StateEstimate:
    return StateEstimate(
        state="HYPER",
        confidence=confidence,
        scores=StateScores(flow=0.05, hypo=0.05, hyper=max(confidence, 0.05), recovery=0.0),
        reasons=["test"],
        signal_quality=SignalQuality(physio=physio, kinematics=kinematics, telemetry=telemetry),
        timestamp=0.0,
        dwell_seconds=dwell,
        evidence_coverage=coverage,
    )


def _flow(dwell: float = 10.0) -> StateEstimate:
    return StateEstimate(
        state="FLOW",
        confidence=0.85,
        scores=StateScores(flow=0.85, hypo=0.05, hyper=0.05, recovery=0.0),
        reasons=["test"],
        signal_quality=SignalQuality(physio=0.9, kinematics=0.9, telemetry=0.9),
        timestamp=0.0,
        dwell_seconds=dwell,
    )


def _policy(tmp_path: Path, **overrides: object) -> TriggerPolicy:
    config = InterventionConfig(
        receptivity_enforced=False,
        cooldown_seconds=0,
        max_interventions_per_hour=0,
        **overrides,  # type: ignore[arg-type]
    )
    return TriggerPolicy(
        config=config,
        state_config=StateConfig(hyper_dwell_seconds=30),
        dismissal_model_path=tmp_path / "dismissal_model.json",
        quiet_mode_history_path=tmp_path / "quiet_mode_history.json",
    )


# ---------------------------------------------------------------------------
# D1 — inactivity is measured from the last input, bounded by exposure
# ---------------------------------------------------------------------------


def test_d1_forty_seconds_of_silence_is_observed_as_inactivity() -> None:
    hooks = InputHooks(clock=FakeClock())
    aggregator = FeatureAggregator(hooks)
    # One keystroke, then silence. The 15 s sliding window prunes the event
    # long before 40 s have elapsed; the last-input marker must survive it.
    hooks.record_key_event(KeyType.REGULAR, timestamp=1_000.0)
    aggregator.build_features(window_seconds=15.0, current_time=1_000.5)
    later = aggregator.build_features(window_seconds=15.0, current_time=1_040.0)
    assert later.inactivity_seconds == pytest.approx(40.0)
    assert later.key_press_count == 0  # the event itself left the window


def test_d1_inactivity_is_bounded_by_exposure_not_machine_uptime() -> None:
    hooks = InputHooks(clock=FakeClock())
    aggregator = FeatureAggregator(hooks)
    # No events ever, first observation at a large monotonic instant.
    first = aggregator.build_features(window_seconds=15.0, current_time=50_000.0)
    later = aggregator.build_features(window_seconds=15.0, current_time=50_040.0)
    assert first.inactivity_seconds == 0.0
    assert later.inactivity_seconds == pytest.approx(40.0)


def test_d1_forty_seconds_of_silence_yields_under_engaged_support() -> None:
    scorer = RuleScorer()
    evaluation = scorer.evaluate(
        _vector(
            {
                FeatureName.INACTIVITY_SECONDS: 40.0,
                FeatureName.KEYPRESS_RATE_PER_MIN: 0.0,
                FeatureName.CLICK_FREQUENCY: 0.0,
                FeatureName.MOUSE_VELOCITY_MEAN: 0.0,
            }
        )
    )
    assert evaluation.scores.under_engaged > 0.0
    # 14 s of silence stays below the 30 s transform floor.
    quiet_but_recent = scorer.evaluate(
        _vector(
            {
                FeatureName.INACTIVITY_SECONDS: 14.0,
                FeatureName.KEYPRESS_RATE_PER_MIN: 0.0,
                FeatureName.CLICK_FREQUENCY: 0.0,
                FeatureName.MOUSE_VELOCITY_MEAN: 0.0,
            }
        )
    )
    assert quiet_but_recent.scores.under_engaged == 0.0


# ---------------------------------------------------------------------------
# D2 — zero mouse-variance baseline
# ---------------------------------------------------------------------------


def test_d2_zero_mouse_variance_baseline_does_not_break_evaluation() -> None:
    scorer = RuleScorer(baselines=UserBaselines(mouse_variance_baseline=0.0))
    evaluation = scorer.evaluate(
        _vector(
            {
                FeatureName.MOUSE_VELOCITY_MEAN: 1_200.0,
                FeatureName.MOUSE_VELOCITY_VARIANCE: 80_000.0,
                FeatureName.CLICK_FREQUENCY: 3.0,
                FeatureName.KEYPRESS_RATE_PER_MIN: 90.0,
                FeatureName.KEYSTROKE_INTERVAL_VARIANCE: 8_000.0,
                FeatureName.CORRECTION_RATE_PER_100_KEYS: 25.0,
                FeatureName.INACTIVITY_SECONDS: 0.5,
                FeatureName.TAB_SWITCH_RATE_PER_MIN: 40.0,
                FeatureName.SCROLL_BACK_RATE_PER_MIN: 40.0,
                FeatureName.THRASHING_SCORE: 1.0,
            }
        )
    )
    assert evaluation.status == "estimated"
    assert 0.0 < evaluation.scores.support_likely <= 1.0


# ---------------------------------------------------------------------------
# D3 — dwell counts time above the trigger gate, not label age
# ---------------------------------------------------------------------------


def test_d3_spike_cannot_borrow_dwell_from_sub_gate_hyper_label(tmp_path: Path) -> None:
    policy = _policy(tmp_path, adaptive_threshold_enabled=False)
    # The HYPER label persists at a confidence far below the 0.70 gate.
    t = 0.0
    for _ in range(100):
        decision = policy.evaluate(_hyper(confidence=0.30, dwell=t), current_time=t)
        assert decision.should_trigger is False
        t += 1.0
    # A spike crosses the gate with 100 s of label age behind it.
    spike = policy.evaluate(_hyper(confidence=0.75, dwell=t), current_time=t)
    assert spike.should_trigger is False
    assert "above gate" in spike.reason
    # It still cannot fire 2.5 s later ...
    t += 2.5
    assert policy.evaluate(_hyper(confidence=0.75, dwell=t), current_time=t).should_trigger is False
    # ... but does once the confidence has been above the gate for 30 s.
    t += 28.0
    assert policy.evaluate(_hyper(confidence=0.75, dwell=t), current_time=t).should_trigger is True


def test_d3_dip_below_gate_resets_the_above_gate_dwell(tmp_path: Path) -> None:
    policy = _policy(tmp_path, adaptive_threshold_enabled=False)
    t = 0.0
    for _ in range(25):
        policy.evaluate(_hyper(confidence=0.8, dwell=t), current_time=t)
        t += 1.0
    policy.evaluate(_hyper(confidence=0.5, dwell=t), current_time=t)  # dip below 0.70
    t += 1.0
    for _ in range(10):
        assert policy.evaluate(_hyper(confidence=0.8, dwell=t), current_time=t).should_trigger is False
        t += 1.0
    t += 20.0
    assert policy.evaluate(_hyper(confidence=0.8, dwell=t), current_time=t).should_trigger is True


def test_d3_smoother_exits_to_unknown_after_exit_dwell() -> None:
    config = StateConfig(
        ema_alpha=1.0,
        estimate_entry_threshold=0.4,
        estimate_exit_threshold=0.25,
        hyper_dwell_seconds=1,
        flow_dwell_seconds=1,
        hypo_dwell_seconds=1,
    )
    quality = SignalQuality(telemetry=1.0)
    smoother = ScoreSmoother(config)
    smoother.update(StateScores(hyper=0.6), quality, timestamp=0.0)
    assert smoother.update(StateScores(hyper=0.6), quality, timestamp=1.5).state == "HYPER"
    # Support collapses below the exit threshold while nothing else clears
    # its entry threshold: the label used to persist indefinitely.
    weak = StateScores(flow=0.3, hyper=0.2)
    t = 2.0
    while t < 2.0 + EXIT_TO_UNKNOWN_DWELL_SECONDS - 0.5:
        assert smoother.update(weak, quality, timestamp=t).state == "HYPER"
        t += 0.5
    assert smoother.update(weak, quality, timestamp=2.5 + EXIT_TO_UNKNOWN_DWELL_SECONDS).state == "UNKNOWN"
    assert smoother.transitions[-1].to_state == "UNKNOWN"


def test_d3_smoother_exit_dwell_does_not_preempt_recovery() -> None:
    config = StateConfig(
        ema_alpha=1.0,
        estimate_entry_threshold=0.4,
        estimate_exit_threshold=0.25,
        hyper_dwell_seconds=1,
        flow_dwell_seconds=1,
        hypo_dwell_seconds=1,
    )
    quality = SignalQuality(telemetry=1.0)
    smoother = ScoreSmoother(config)
    smoother.update(StateScores(hyper=0.6), quality, timestamp=0.0)
    assert smoother.update(StateScores(hyper=0.6), quality, timestamp=1.5).state == "HYPER"
    strong_flow = StateScores(flow=0.8, hyper=0.0)
    smoother.update(strong_flow, quality, timestamp=2.0)
    assert smoother.update(strong_flow, quality, timestamp=7.5).state == "RECOVERY"


# ---------------------------------------------------------------------------
# D4 — the configured base threshold is never clipped upward
# ---------------------------------------------------------------------------


def test_d4_overlay_threshold_below_adaptive_floor_is_honoured(tmp_path: Path) -> None:
    policy = _policy(tmp_path, overlay_threshold=0.55, adaptive_threshold_enabled=True)
    decision = policy.evaluate(_hyper(confidence=0.60, dwell=60.0), current_time=100.0)
    assert decision.effective_threshold == pytest.approx(0.55)
    assert decision.should_trigger is True


def test_d4_settings_slider_update_is_honoured_live(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    assert policy.evaluate(_hyper(confidence=0.60, dwell=60.0), current_time=1.0).should_trigger is False
    policy.update_thresholds(
        InterventionConfig(
            receptivity_enforced=False,
            cooldown_seconds=0,
            max_interventions_per_hour=0,
            overlay_threshold=0.55,
        )
    )
    decision = policy.evaluate(_hyper(confidence=0.60, dwell=60.0), current_time=2.0)
    assert decision.effective_threshold == pytest.approx(0.55)


def test_d4_adaptive_bounds_still_cap_the_adaptive_terms(tmp_path: Path) -> None:
    policy = _policy(tmp_path, overlay_threshold=0.85)
    # Ten approvals pull the threshold down, but never below the adaptive floor.
    for _ in range(12):
        policy.record_outcome(dismissed=False)
    decision = policy.evaluate(_hyper(confidence=0.9, dwell=60.0), current_time=1.0)
    assert decision.effective_threshold == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# D5 — shared interruption gate
# ---------------------------------------------------------------------------


def test_d5_shared_gate_blocks_quiet_mode_receptivity_and_cap(tmp_path: Path) -> None:
    policy = TriggerPolicy(
        config=InterventionConfig(cooldown_seconds=0, max_interventions_per_hour=2),
        state_config=StateConfig(),
        dismissal_model_path=tmp_path / "d.json",
        quiet_mode_history_path=tmp_path / "q.json",
    )
    open_gate = policy.check_interruption_gate(current_time=10.0)
    assert isinstance(open_gate, InterruptionGateDecision) and open_gate.allowed

    mic = policy.check_interruption_gate(current_time=10.0, mic_active=True)
    assert mic.allowed is False and mic.receptivity_blocked is True

    policy.activate_quiet_mode(duration_minutes=15, current_time=10.0)
    quiet = policy.check_interruption_gate(current_time=11.0)
    assert quiet.allowed is False and quiet.quiet_mode_active is True
    policy.clear_quiet_mode()

    policy.record_intervention(timestamp=12.0)
    policy.record_intervention(timestamp=13.0)
    capped = policy.check_interruption_gate(current_time=14.0)
    assert capped.allowed is False and "cap" in capped.reason.lower()


# ---------------------------------------------------------------------------
# D8 — dismissal model pauses visibly and recovers without reset()
# ---------------------------------------------------------------------------


def test_d8_dismissal_model_pause_is_visible_time_boxed_and_recoverable(tmp_path: Path) -> None:
    policy = _policy(tmp_path, quiet_mode_minutes=30, adaptive_threshold_enabled=False)
    # Train the online model hard toward "this user dismisses".
    for _ in range(80):
        policy.record_outcome(dismissed=True, confidence=0.9, context_complexity=0.5)
    assert policy._dismissal_outcomes >= DISMISSAL_MODEL_MIN_OUTCOMES
    estimate = _hyper(confidence=0.9, dwell=120.0)

    paused = policy.evaluate(estimate, current_time=1_000.0)
    assert paused.should_trigger is False
    assert "Paused after" in paused.reason and "resume" in paused.reason
    assert paused.paused_until_unix_ms is not None
    assert paused.dismissal_probability is not None and paused.dismissal_probability > 0.6
    # The reason names a wall-clock resume time.
    datetime.strptime(paused.reason.rsplit(" ", 1)[-1], "%H:%M")

    still_paused = policy.evaluate(estimate, current_time=1_600.0)
    assert still_paused.should_trigger is False and "Paused after" in still_paused.reason

    # Recovery path without reset(): every pause is time-boxed, ends with a
    # probe proposal, and an approval retrains the (decayed) model. Within a
    # handful of cycles proposals flow again on their own.
    t = 1_000.0
    recovered = False
    for _cycle in range(6):
        t += 4 * 30 * 60 + 1  # longer than the longest possible pause level
        decision = policy.evaluate(estimate, current_time=t)
        assert decision.should_trigger is True, decision.reason  # probe or open
        policy.record_intervention(timestamp=t)
        policy.record_outcome(dismissed=False, confidence=0.9, context_complexity=0.5)
        t += 5.0
        follow_up = policy.evaluate(estimate, current_time=t)
        if follow_up.should_trigger:
            recovered = True
            break
        # Not yet: the pause must be visible and time-boxed, never opaque.
        assert "Paused after" in follow_up.reason and follow_up.paused_until_unix_ms is not None
    assert recovered, "approvals never reopened proposals"
    assert policy._dismissal_pause_level == 0


def test_d8_dismissal_is_not_penalised_twice_at_once(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    base = policy.evaluate(_hyper(confidence=0.9, dwell=60.0), current_time=1.0).effective_threshold
    policy.record_dismissal(timestamp=1.0)
    policy.record_outcome(dismissed=True)
    bumped = policy.evaluate(_hyper(confidence=0.9, dwell=60.0), current_time=2.0).effective_threshold
    # One dismissal: the +0.05 time-boxed bump only, not bump + 0.01 offset.
    assert bumped == pytest.approx(base + 0.05)
    # Once the bump expires the long-run +0.01 increment applies instead.
    expired = policy.evaluate(_hyper(confidence=0.9, dwell=60.0), current_time=2.0 + 3_600.0 + 1)
    assert expired.effective_threshold == pytest.approx(base + 0.01)


# ---------------------------------------------------------------------------
# D10 — weekly schedule "quiet" slots
# ---------------------------------------------------------------------------


def test_d10_weekly_schedule_quiet_slot_blocks_like_quiet_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy(tmp_path)
    monkeypatch.setattr(policy, "lookup_schedule_slot", lambda **_kw: "quiet")
    decision = policy.evaluate(_hyper(confidence=0.9, dwell=60.0), current_time=1.0)
    assert decision.should_trigger is False
    assert decision.reason == "weekly_schedule_quiet"
    assert decision.quiet_mode_active is True
    gate = policy.check_interruption_gate(current_time=1.0)
    assert gate.allowed is False and gate.quiet_mode_active is True


# ---------------------------------------------------------------------------
# D11 — camera absence cannot veto behaviour-only evidence
# ---------------------------------------------------------------------------


def test_d11_camera_off_with_telemetry_coverage_can_trigger(tmp_path: Path) -> None:
    policy = _policy(tmp_path, adaptive_threshold_enabled=False)
    estimate = _hyper(
        confidence=0.92, dwell=60.0, physio=0.0, kinematics=0.0, telemetry=0.9, coverage=0.6,
    )
    assert estimate.signal_quality.acceptable is False
    decision = policy.evaluate(estimate, current_time=100.0)
    assert decision.should_trigger is True, decision.reason
    assert policy.hyper_eligible(estimate, current_time=100.0) is True
