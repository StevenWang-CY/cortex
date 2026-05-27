"""
Tests for P0 §3.5 HYPO behavioural-conjunction gate.

The gate's contract:

- HYPO state must be sustained at least ``hypo_dwell_seconds`` (default
  60s) AND keystroke_rate < 5/min AND scroll_rate < 1/min AND (if HR
  delta is available) HR delta < -5 % from baseline.
- A user typing or scrolling actively is **engaged**; firing a "you're
  drifting" intervention on them is a false positive worse than a HYPER
  false positive.
- The recovery reinforcer fires at most once per window.
"""

from __future__ import annotations

from cortex.libs.config.settings import InterventionConfig, StateConfig
from cortex.libs.schemas.state import (
    SignalQuality,
    StateEstimate,
    StateScores,
)
from cortex.services.state_engine.hypo_detector import (
    HypoGateConfig,
    is_disengaged,
)
from cortex.services.state_engine.recovery_detector import (
    RecoveryGateConfig,
    RecoveryReinforcer,
)
from cortex.services.state_engine.trigger_policy import TriggerPolicy


def _good_sq() -> SignalQuality:
    return SignalQuality(physio=0.9, kinematics=0.9, telemetry=0.9)


def _hypo_estimate(*, dwell: float, confidence: float = 0.9) -> StateEstimate:
    return StateEstimate(
        state="HYPO",
        confidence=confidence,
        scores=StateScores(flow=0.1, hypo=0.8, hyper=0.05, recovery=0.05),
        reasons=["test"],
        signal_quality=_good_sq(),
        timestamp=0.0,
        dwell_seconds=dwell,
    )


def _policy(*, hypo_dwell: float = 60.0) -> TriggerPolicy:
    """Construct a policy with HYPO interventions enabled and a tight cap.

    Sets receptivity_enforced=False so we don't trip on mic/fullscreen in
    the unit test (those are integration-level inputs).
    """
    cfg = InterventionConfig(
        receptivity_enforced=False,
        cooldown_seconds=0,
        enable_hypo_recovery_interventions=True,
    )
    return TriggerPolicy(
        config=cfg,
        state_config=StateConfig(hyper_dwell_seconds=10),
        hypo_dwell_seconds=hypo_dwell,
        recovery_window_seconds=300.0,
    )


# --------------------------------------------------------------------- #
# Pure detector unit tests
# --------------------------------------------------------------------- #


def test_is_disengaged_fires_when_conjunction_holds() -> None:
    fired, reason = is_disengaged(
        None,
        dwell_seconds=90.0,
        baselines=None,
        keystroke_rate=0.0,
        scroll_rate=0.0,
        hr_delta_pct=-0.08,
    )
    assert fired is True
    assert "drift detected" in reason


def test_is_disengaged_skips_hr_when_none() -> None:
    fired, _ = is_disengaged(
        None,
        dwell_seconds=90.0,
        baselines=None,
        keystroke_rate=0.0,
        scroll_rate=0.0,
        hr_delta_pct=None,
    )
    assert fired is True


def test_is_disengaged_rejects_short_dwell() -> None:
    fired, reason = is_disengaged(
        None,
        dwell_seconds=20.0,
        baselines=None,
        keystroke_rate=0.0,
        scroll_rate=0.0,
        hr_delta_pct=-0.08,
    )
    assert fired is False
    assert "dwell" in reason.lower()


def test_is_disengaged_rejects_high_keystroke() -> None:
    fired, reason = is_disengaged(
        None,
        dwell_seconds=120.0,
        baselines=None,
        keystroke_rate=20.0,
        scroll_rate=0.0,
        hr_delta_pct=-0.08,
    )
    assert fired is False
    assert "Keystroke" in reason


def test_is_disengaged_rejects_high_scroll() -> None:
    fired, reason = is_disengaged(
        None,
        dwell_seconds=120.0,
        baselines=None,
        keystroke_rate=0.0,
        scroll_rate=5.0,
        hr_delta_pct=-0.08,
    )
    assert fired is False
    assert "Scroll" in reason


def test_is_disengaged_rejects_hr_above_floor() -> None:
    fired, reason = is_disengaged(
        None,
        dwell_seconds=120.0,
        baselines=None,
        keystroke_rate=0.0,
        scroll_rate=0.0,
        hr_delta_pct=0.05,
    )
    assert fired is False
    assert "HR" in reason


def test_hypo_gate_config_overrides_defaults() -> None:
    tight = HypoGateConfig(
        dwell_seconds=30.0,
        max_keystroke_per_min=2.0,
        max_scroll_per_min=0.5,
        min_hr_delta_pct=-0.10,
    )
    fired, _ = is_disengaged(
        None,
        dwell_seconds=35.0,
        baselines=None,
        keystroke_rate=0.0,
        scroll_rate=0.0,
        hr_delta_pct=-0.12,
        config=tight,
    )
    assert fired is True


# --------------------------------------------------------------------- #
# TriggerPolicy integration tests
# --------------------------------------------------------------------- #


def test_hypo_with_low_keystroke_and_low_scroll_triggers() -> None:
    """HYPO + 60s dwell + 0 keystrokes + 0 scrolls → trigger."""
    p = _policy()
    est = _hypo_estimate(dwell=120.0)
    decision = p.evaluate(
        est,
        context_complexity=0.9,
        keystroke_rate=0.0,
        scroll_rate=0.0,
        hr_delta_pct=-0.08,
        current_time=10.0,
    )
    assert decision.should_trigger is True
    assert "HYPO" in decision.reason


def test_hypo_with_high_keystroke_does_not_trigger() -> None:
    """Deep typing — same state, but keystroke_rate=20 → no trigger."""
    p = _policy()
    est = _hypo_estimate(dwell=120.0)
    decision = p.evaluate(
        est,
        context_complexity=0.9,
        keystroke_rate=20.0,
        scroll_rate=0.0,
        hr_delta_pct=-0.08,
        current_time=10.0,
    )
    assert decision.should_trigger is False
    assert "Keystroke" in decision.reason


def test_hypo_short_dwell_no_trigger() -> None:
    """HYPO dwell only 20s → no trigger."""
    p = _policy()
    est = _hypo_estimate(dwell=20.0)
    decision = p.evaluate(
        est,
        context_complexity=0.9,
        keystroke_rate=0.0,
        scroll_rate=0.0,
        hr_delta_pct=-0.08,
        current_time=10.0,
    )
    assert decision.should_trigger is False
    assert "dwell" in decision.reason.lower()


def test_hypo_respects_low_confidence() -> None:
    """HYPO confidence floor matches HYPER (≥ 0.85 by default)."""
    p = _policy()
    est = _hypo_estimate(dwell=120.0, confidence=0.50)
    decision = p.evaluate(
        est,
        context_complexity=0.9,
        keystroke_rate=0.0,
        scroll_rate=0.0,
        hr_delta_pct=-0.08,
        current_time=10.0,
    )
    assert decision.should_trigger is False
    assert "confidence" in decision.reason.lower()


def test_hypo_disabled_when_feature_flag_off() -> None:
    """Default config has enable_hypo_recovery_interventions=False."""
    cfg = InterventionConfig(receptivity_enforced=False, cooldown_seconds=0)
    p = TriggerPolicy(
        config=cfg,
        state_config=StateConfig(hyper_dwell_seconds=10),
        hypo_dwell_seconds=60.0,
    )
    est = _hypo_estimate(dwell=120.0)
    decision = p.evaluate(
        est,
        context_complexity=0.9,
        keystroke_rate=0.0,
        scroll_rate=0.0,
        hr_delta_pct=-0.08,
        current_time=10.0,
    )
    assert decision.should_trigger is False
    assert "opt-in" in decision.reason


# --------------------------------------------------------------------- #
# Recovery reinforcer tests
# --------------------------------------------------------------------- #


def test_recovery_window_only_one_reinforce() -> None:
    """Within recovery window, second should_reinforce returns False."""
    reinforcer = RecoveryReinforcer(
        RecoveryGateConfig(window_seconds=300.0, reinforce_cooldown_seconds=300.0),
    )
    assert reinforcer.should_reinforce(dwell_seconds=5.0) is True
    assert reinforcer.should_reinforce(dwell_seconds=10.0) is False
    assert reinforcer.should_reinforce(dwell_seconds=50.0) is False


def test_recovery_reinforcer_resets_between_windows() -> None:
    reinforcer = RecoveryReinforcer(
        RecoveryGateConfig(window_seconds=300.0, reinforce_cooldown_seconds=300.0),
    )
    assert reinforcer.should_reinforce(dwell_seconds=1.0) is True
    reinforcer.reset_window()
    assert reinforcer.should_reinforce(dwell_seconds=1.0) is True


def test_policy_fires_recovery_then_silences_within_window() -> None:
    """After a HYPER→FLOW exit, RECOVERY triggers once and then suppresses."""
    p = _policy()
    # 1) seed a HYPER pass so the policy records a HYPER-exit.
    hyper_est = StateEstimate(
        state="HYPER",
        confidence=0.95,
        scores=StateScores(flow=0.05, hypo=0.05, hyper=0.85, recovery=0.05),
        reasons=["test"],
        signal_quality=_good_sq(),
        timestamp=0.0,
        dwell_seconds=20.0,
    )
    p.evaluate(hyper_est, context_complexity=0.9, current_time=0.0)

    # 2) now transition to RECOVERY — should trigger reinforcement once.
    recovery_est = StateEstimate(
        state="RECOVERY",
        confidence=0.9,
        scores=StateScores(flow=0.05, hypo=0.05, hyper=0.05, recovery=0.85),
        reasons=["test"],
        signal_quality=_good_sq(),
        timestamp=0.0,
        dwell_seconds=5.0,
    )
    d1 = p.evaluate(recovery_est, context_complexity=0.9, current_time=5.0)
    assert d1.should_trigger is True
    assert "RECOVERY" in d1.reason

    # 3) second evaluation inside the window — already reinforced.
    d2 = p.evaluate(recovery_est, context_complexity=0.9, current_time=10.0)
    assert d2.should_trigger is False
    assert "already" in d2.reason.lower() or "reinforcement" in d2.reason.lower()


def test_flow_state_never_triggers() -> None:
    """FLOW must yield should_trigger=False with the right rejection reason."""
    p = _policy()
    flow_est = StateEstimate(
        state="FLOW",
        confidence=0.99,
        scores=StateScores(flow=0.95, hypo=0.0, hyper=0.0, recovery=0.05),
        reasons=["test"],
        signal_quality=_good_sq(),
        timestamp=0.0,
        dwell_seconds=600.0,
    )
    decision = p.evaluate(flow_est, context_complexity=0.9, current_time=1.0)
    assert decision.should_trigger is False
    assert "FLOW" in decision.reason
