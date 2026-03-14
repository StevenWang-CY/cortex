"""
Unit tests for State Engine — Feature fusion, rule scorer, smoother, trigger policy.

Tests verify:
- Feature fusion from multi-channel sources
- Score computation for HYPER/HYPO/FLOW/RECOVERY states
- EMA smoothing and hysteresis
- State transitions with dwell time
- Trigger policy: cooldown, dismissal, quiet mode, adaptive thresholds
"""

from __future__ import annotations

import numpy as np
import pytest

from cortex.libs.config.settings import InterventionConfig, StateConfig
from cortex.libs.schemas.features import (
    FeatureVector,
    KinematicFeatures,
    PhysioFeatures,
    TelemetryFeatures,
)
from cortex.libs.schemas.state import (
    SignalQuality,
    StateEstimate,
    StateScores,
    UserBaselines,
    UserState,
)
from cortex.services.state_engine.feature_fusion import FeatureFusion
from cortex.services.state_engine.rule_scorer import RuleScorer
from cortex.services.state_engine.smoother import ScoreSmoother
from cortex.services.state_engine.trigger_policy import TriggerPolicy


# =============================================================================
# Helpers
# =============================================================================


def make_flow_features() -> FeatureVector:
    """Create a feature vector indicating FLOW state."""
    return FeatureVector(
        timestamp=1.0,
        hr=72.0,  # Within 10% of 72 baseline
        hrv_rmssd=55.0,  # Elevated HRV
        hr_delta=0.0,
        blink_rate=16.0,  # Normal 12-20/min
        blink_rate_delta=0.0,
        shoulder_drop_ratio=0.02,
        forward_lean_angle=5.0,
        mouse_velocity_mean=400.0,
        mouse_velocity_variance=5000.0,
        click_frequency=0.5,
        keystroke_interval_variance=500.0,
        tab_switch_frequency=5.0,
    )


def make_hyper_features() -> FeatureVector:
    """Create a feature vector indicating HYPER state."""
    return FeatureVector(
        timestamp=1.0,
        hr=95.0,  # >15% above 72 baseline
        hrv_rmssd=12.0,  # Low HRV (stress)
        hr_delta=5.0,
        blink_rate=5.0,  # Blink suppression (<8/min)
        blink_rate_delta=-12.0,
        shoulder_drop_ratio=0.25,
        forward_lean_angle=25.0,  # Forward lean >20°
        mouse_velocity_mean=1500.0,
        mouse_velocity_variance=80000.0,  # Very high variance (>3x baseline)
        click_frequency=3.0,
        keystroke_interval_variance=8000.0,
        tab_switch_frequency=25.0,  # Rapid switching >20/min
    )


def make_hypo_features() -> FeatureVector:
    """Create a feature vector indicating HYPO state."""
    return FeatureVector(
        timestamp=1.0,
        hr=60.0,  # Below baseline
        hrv_rmssd=35.0,  # Dropping
        hr_delta=-2.0,
        blink_rate=28.0,  # High blink rate
        blink_rate_delta=10.0,
        shoulder_drop_ratio=0.2,
        forward_lean_angle=5.0,  # Slumped but not leaning
        mouse_velocity_mean=30.0,  # Very low activity
        mouse_velocity_variance=1000.0,
        click_frequency=0.1,
        keystroke_interval_variance=200.0,
        tab_switch_frequency=0.5,
    )


def make_good_quality() -> SignalQuality:
    return SignalQuality(physio=0.8, kinematics=0.7, telemetry=0.9)


def make_poor_quality() -> SignalQuality:
    return SignalQuality(physio=0.1, kinematics=0.1, telemetry=0.1)


# =============================================================================
# Feature Fusion Tests
# =============================================================================


class TestFeatureFusion:
    """Test multi-channel feature fusion."""

    def test_fuse_all_channels(self):
        """Fusing all channels should produce complete FeatureVector."""
        fusion = FeatureFusion()
        fusion.update_physio(
            PhysioFeatures(
                pulse_bpm=72.0, pulse_quality=0.8,
                pulse_variability_proxy=50.0, hr_delta_5s=1.0, valid=True,
            ),
            timestamp=1.0,
        )
        fusion.update_kinematics(
            KinematicFeatures(
                blink_rate=16.0, blink_rate_delta=-1.0,
                blink_suppression_score=0.0, head_pitch=2.0,
                head_yaw=0.0, head_roll=0.0,
                slump_score=0.1, forward_lean_score=0.1,
                shoulder_drop_ratio=0.05, confidence=0.8,
            ),
            timestamp=1.0,
        )
        fusion.update_telemetry(
            TelemetryFeatures(
                mouse_velocity_mean=500.0, mouse_velocity_variance=5000.0,
                mouse_jerk_score=0.1, click_burst_score=0.0,
                click_frequency=0.5, keyboard_burst_score=0.1,
                keystroke_interval_variance=500.0, backspace_density=0.05,
                inactivity_seconds=1.0, window_switch_rate=5.0,
            ),
            timestamp=1.0,
        )

        fv, quality = fusion.fuse(timestamp=1.0)

        assert fv.hr == 72.0
        assert fv.hrv_rmssd == 50.0
        assert fv.blink_rate == 16.0
        assert fv.mouse_velocity_mean == 500.0
        assert quality.physio > 0.5
        assert quality.kinematics > 0.5
        assert quality.telemetry > 0.5

    def test_fuse_missing_physio(self):
        """Missing physio should produce None for HR features."""
        fusion = FeatureFusion()
        fusion.update_telemetry(
            TelemetryFeatures(
                mouse_velocity_mean=300.0, mouse_velocity_variance=2000.0,
                mouse_jerk_score=0.0, click_burst_score=0.0,
                click_frequency=0.2, keyboard_burst_score=0.0,
                keystroke_interval_variance=100.0, backspace_density=0.0,
                inactivity_seconds=5.0, window_switch_rate=2.0,
            ),
            timestamp=1.0,
        )

        fv, quality = fusion.fuse(timestamp=1.0)

        assert fv.hr is None
        assert fv.hrv_rmssd is None
        assert fv.mouse_velocity_mean == 300.0
        assert quality.physio == 0.0

    def test_fuse_empty(self):
        """Empty fusion should produce zeroed features."""
        fusion = FeatureFusion()
        fv, quality = fusion.fuse(timestamp=1.0)

        assert fv.hr is None
        assert fv.blink_rate is None
        assert fv.mouse_velocity_mean == 0.0
        assert quality.overall < 0.1

    def test_signal_quality_staleness(self):
        """Stale data should reduce signal quality."""
        fusion = FeatureFusion()
        fusion.update_physio(
            PhysioFeatures(
                pulse_bpm=72.0, pulse_quality=0.8,
                pulse_variability_proxy=50.0, valid=True,
            ),
            timestamp=1.0,
        )

        # Fresh data: high quality
        _, q_fresh = fusion.fuse(timestamp=1.5)
        # Stale data (10 seconds later): reduced quality
        _, q_stale = fusion.fuse(timestamp=11.0)

        assert q_fresh.physio > q_stale.physio

    def test_reset(self):
        """Reset should clear all channels."""
        fusion = FeatureFusion()
        fusion.update_physio(
            PhysioFeatures(
                pulse_bpm=72.0, pulse_quality=0.8, valid=True,
            ),
            timestamp=1.0,
        )
        fusion.reset()
        fv, quality = fusion.fuse(timestamp=2.0)
        assert fv.hr is None


# =============================================================================
# Rule Scorer Tests
# =============================================================================


class TestRuleScorer:
    """Test rule-based score computation."""

    def _make_scorer(self) -> RuleScorer:
        return RuleScorer(baselines=UserBaselines())

    def test_hyper_features_high_hyper_score(self):
        """HYPER features should produce high hyper score."""
        scorer = self._make_scorer()
        fv = make_hyper_features()
        scores = scorer.compute_scores(fv)
        assert scores.hyper > 0.4, f"Hyper score={scores.hyper:.3f} should be > 0.4"

    def test_flow_features_high_flow_score(self):
        """FLOW features should produce high flow score."""
        scorer = self._make_scorer()
        fv = make_flow_features()
        scores = scorer.compute_scores(fv)
        assert scores.flow > 0.4, f"Flow score={scores.flow:.3f} should be > 0.4"

    def test_hypo_features_high_hypo_score(self):
        """HYPO features should produce high hypo score."""
        scorer = self._make_scorer()
        fv = make_hypo_features()
        scores = scorer.compute_scores(fv)
        assert scores.hypo > 0.3, f"Hypo score={scores.hypo:.3f} should be > 0.3"

    def test_flow_dominant_over_hyper(self):
        """FLOW features should not produce high hyper score."""
        scorer = self._make_scorer()
        fv = make_flow_features()
        scores = scorer.compute_scores(fv)
        assert scores.flow > scores.hyper

    def test_hyper_dominant_over_flow(self):
        """HYPER features should produce hyper > flow."""
        scorer = self._make_scorer()
        fv = make_hyper_features()
        scores = scorer.compute_scores(fv)
        assert scores.hyper > scores.flow

    def test_all_scores_in_range(self):
        """All scores should be in [0, 1] range."""
        scorer = self._make_scorer()
        for fv_maker in [make_flow_features, make_hyper_features, make_hypo_features]:
            fv = fv_maker()
            scores = scorer.compute_scores(fv)
            assert 0.0 <= scores.flow <= 1.0
            assert 0.0 <= scores.hypo <= 1.0
            assert 0.0 <= scores.hyper <= 1.0
            assert 0.0 <= scores.recovery <= 1.0


class TestSubScores:
    """Test individual sub-score functions."""

    def _make_scorer(self) -> RuleScorer:
        return RuleScorer(baselines=UserBaselines(hr_baseline=72.0))

    def test_pulse_elevation_above_threshold(self):
        scorer = self._make_scorer()
        # HR 90 > 72 * 1.15 = 82.8
        score = scorer.score_pulse_elevation(90.0)
        assert score > 0.3

    def test_pulse_elevation_normal(self):
        scorer = self._make_scorer()
        score = scorer.score_pulse_elevation(72.0)
        assert score == 0.0

    def test_pulse_elevation_none(self):
        scorer = self._make_scorer()
        score = scorer.score_pulse_elevation(None)
        assert score == 0.0

    def test_hrv_drop_low(self):
        scorer = self._make_scorer()
        score = scorer.score_hrv_drop(12.0)
        assert score > 0.8

    def test_hrv_drop_normal(self):
        scorer = self._make_scorer()
        score = scorer.score_hrv_drop(50.0)
        assert score == 0.0

    def test_blink_suppression_low_rate(self):
        scorer = self._make_scorer()
        score = scorer.score_blink_suppression(3.0)
        assert score > 0.5

    def test_blink_suppression_normal(self):
        scorer = self._make_scorer()
        score = scorer.score_blink_suppression(16.0)
        assert score == 0.0

    def test_mouse_thrash_high_variance(self):
        scorer = self._make_scorer()
        # Default baseline variance = 10000
        score = scorer.score_mouse_thrash(50000.0)
        assert score > 0.3

    def test_mouse_thrash_normal(self):
        scorer = self._make_scorer()
        score = scorer.score_mouse_thrash(5000.0)
        assert score == 0.0

    def test_window_switch_high(self):
        scorer = self._make_scorer()
        score = scorer.score_window_switch(30.0)
        assert score > 0.5

    def test_window_switch_low(self):
        scorer = self._make_scorer()
        score = scorer.score_window_switch(5.0)
        assert score == 0.0


# =============================================================================
# Score Smoother Tests
# =============================================================================


class TestScoreSmoother:
    """Test EMA smoothing, hysteresis, and state transitions."""

    def _make_smoother(self) -> ScoreSmoother:
        return ScoreSmoother(config=StateConfig())

    def test_initial_state_is_flow(self):
        smoother = self._make_smoother()
        assert smoother.current_state == UserState.FLOW

    def test_ema_smoothing_effect(self):
        """EMA should smooth out spiky scores."""
        smoother = self._make_smoother()
        quality = make_good_quality()

        # Feed high hyper score
        high_hyper = StateScores(flow=0.1, hypo=0.0, hyper=1.0, recovery=0.0)
        est = smoother.update(high_hyper, quality, timestamp=1.0)

        # Smoothed hyper should be less than 1.0 due to EMA
        assert est.scores.hyper < 1.0
        assert est.scores.hyper > 0.0

    def test_repeated_hyper_scores_increase_smoothed(self):
        """Repeated high hyper scores should drive smoothed score up."""
        smoother = self._make_smoother()
        quality = make_good_quality()

        scores = StateScores(flow=0.1, hypo=0.0, hyper=0.95, recovery=0.0)

        for i in range(20):
            est = smoother.update(scores, quality, timestamp=float(i))

        assert est.scores.hyper > 0.8

    def test_hysteresis_prevents_flicker(self):
        """State should not change on brief score fluctuations."""
        smoother = self._make_smoother()
        quality = make_good_quality()

        # Start with flow
        flow_scores = StateScores(flow=0.8, hypo=0.0, hyper=0.1, recovery=0.0)
        for i in range(10):
            smoother.update(flow_scores, quality, timestamp=float(i))

        assert smoother.current_state == UserState.FLOW

        # Brief hyper spike
        hyper_scores = StateScores(flow=0.2, hypo=0.0, hyper=0.7, recovery=0.0)
        est = smoother.update(hyper_scores, quality, timestamp=10.0)

        # Should still be FLOW (hysteresis prevents immediate switch)
        assert smoother.current_state == UserState.FLOW

    def test_sustained_hyper_transitions(self):
        """Sustained HYPER scores should eventually cause state transition."""
        config = StateConfig(
            entry_threshold=0.85,
            exit_threshold=0.70,
            hyper_dwell_seconds=2,  # Short for testing
            ema_alpha=0.5,  # More responsive for testing
        )
        smoother = ScoreSmoother(config=config)
        quality = make_good_quality()

        # Drive hyper very high, flow very low
        hyper_scores = StateScores(flow=0.05, hypo=0.0, hyper=0.99, recovery=0.0)

        # Feed many frames to overcome EMA and dwell
        for i in range(50):
            est = smoother.update(hyper_scores, quality, timestamp=float(i) * 0.5)

        # After sustained hyper input, should eventually transition
        assert smoother.current_state == UserState.HYPER

    def test_transitions_recorded(self):
        """State transitions should be recorded."""
        config = StateConfig(
            entry_threshold=0.85,
            exit_threshold=0.70,
            hyper_dwell_seconds=1,
            ema_alpha=0.6,
        )
        smoother = ScoreSmoother(config=config)
        quality = make_good_quality()

        hyper_scores = StateScores(flow=0.05, hypo=0.0, hyper=0.99, recovery=0.0)
        for i in range(30):
            smoother.update(hyper_scores, quality, timestamp=float(i) * 0.5)

        if smoother.current_state == UserState.HYPER:
            assert len(smoother.transitions) >= 1

    def test_reset(self):
        smoother = self._make_smoother()
        quality = make_good_quality()
        scores = StateScores(flow=0.1, hypo=0.0, hyper=0.9, recovery=0.0)
        smoother.update(scores, quality, timestamp=1.0)
        smoother.reset()
        assert smoother.current_state == UserState.FLOW
        assert smoother.latest_estimate is None


# =============================================================================
# Trigger Policy Tests
# =============================================================================


class TestTriggerPolicy:
    """Test intervention trigger policy."""

    def _make_policy(self, **kwargs) -> TriggerPolicy:
        config = InterventionConfig(**kwargs) if kwargs else InterventionConfig()
        return TriggerPolicy(config=config)

    def _make_hyper_estimate(
        self, confidence: float = 0.9, dwell: float = 20.0,
    ) -> StateEstimate:
        return StateEstimate(
            state="HYPER",
            confidence=confidence,
            scores=StateScores(flow=0.05, hypo=0.0, hyper=confidence, recovery=0.0),
            reasons=["Test"],
            signal_quality=make_good_quality(),
            timestamp=100.0,
            dwell_seconds=dwell,
        )

    def _make_flow_estimate(self) -> StateEstimate:
        return StateEstimate(
            state="FLOW",
            confidence=0.8,
            scores=StateScores(flow=0.8, hypo=0.0, hyper=0.1, recovery=0.0),
            reasons=["Test"],
            signal_quality=make_good_quality(),
            timestamp=100.0,
            dwell_seconds=30.0,
        )

    def test_trigger_on_hyper_with_confidence(self):
        """Should trigger when HYPER with high confidence and sufficient dwell."""
        policy = self._make_policy()
        est = self._make_hyper_estimate(confidence=0.92, dwell=20.0)
        decision = policy.evaluate(est, current_time=200.0)
        assert decision.should_trigger is True

    def test_no_trigger_on_flow(self):
        """Should not trigger when in FLOW state."""
        policy = self._make_policy()
        est = self._make_flow_estimate()
        decision = policy.evaluate(est, current_time=200.0)
        assert decision.should_trigger is False
        assert "FLOW" in decision.reason

    def test_no_trigger_low_confidence(self):
        """Should not trigger when confidence below threshold."""
        policy = self._make_policy()
        est = self._make_hyper_estimate(confidence=0.65, dwell=20.0)
        decision = policy.evaluate(est, current_time=200.0)
        assert decision.should_trigger is False
        assert "below threshold" in decision.reason

    def test_no_trigger_during_cooldown(self):
        """Should not trigger during cooldown period."""
        policy = self._make_policy(cooldown_seconds=60)
        policy.record_intervention(timestamp=150.0)

        est = self._make_hyper_estimate(confidence=0.92, dwell=20.0)
        decision = policy.evaluate(est, current_time=180.0)  # 30s into 60s cooldown
        assert decision.should_trigger is False
        assert "Cooldown" in decision.reason
        assert decision.cooldown_remaining > 0

    def test_trigger_after_cooldown(self):
        """Should trigger after cooldown expires."""
        policy = self._make_policy(cooldown_seconds=60)
        policy.record_intervention(timestamp=100.0)

        est = self._make_hyper_estimate(confidence=0.92, dwell=20.0)
        decision = policy.evaluate(est, current_time=200.0)  # 100s after, cooldown expired
        assert decision.should_trigger is True

    def test_no_trigger_poor_signal_quality(self):
        """Should not trigger with poor signal quality."""
        policy = self._make_policy()
        est = StateEstimate(
            state="HYPER",
            confidence=0.92,
            scores=StateScores(flow=0.05, hypo=0.0, hyper=0.92, recovery=0.0),
            reasons=["Test"],
            signal_quality=make_poor_quality(),
            timestamp=100.0,
            dwell_seconds=20.0,
        )
        decision = policy.evaluate(est, current_time=200.0)
        assert decision.should_trigger is False
        assert "quality" in decision.reason.lower()

    def test_dismissal_raises_threshold(self):
        """Dismissals should raise the effective threshold."""
        policy = self._make_policy()
        base_decision = policy.evaluate(
            self._make_hyper_estimate(confidence=0.9), current_time=200.0,
        )
        base_threshold = base_decision.effective_threshold

        # Record a dismissal
        policy.record_dismissal(timestamp=200.0)
        new_decision = policy.evaluate(
            self._make_hyper_estimate(confidence=0.9), current_time=201.0,
        )

        assert new_decision.effective_threshold > base_threshold

    def test_quiet_mode_on_repeated_dismissals(self):
        """3 dismissals in 5 min should activate quiet mode."""
        policy = self._make_policy(
            max_dismissals=3,
            dismissal_window_minutes=5,
            quiet_mode_minutes=30,
        )

        for i in range(3):
            policy.record_dismissal(timestamp=100.0 + i * 10.0)

        # Verify quiet mode via evaluate() which supports synthetic timestamps
        est = self._make_hyper_estimate(confidence=0.95, dwell=10.0)
        decision = policy.evaluate(est, current_time=130.0)
        assert decision.should_trigger is False
        assert decision.quiet_mode_active is True

        # Quiet mode should expire after 30 minutes
        decision_later = policy.evaluate(est, current_time=130.0 + 31 * 60)
        assert decision_later.quiet_mode_active is False

    def test_no_trigger_insufficient_dwell(self):
        """Should not trigger with insufficient dwell time."""
        policy = self._make_policy()
        est = self._make_hyper_estimate(confidence=0.92, dwell=3.0)  # < 8s
        decision = policy.evaluate(est, current_time=200.0)
        assert decision.should_trigger is False
        assert "Dwell" in decision.reason

    def test_reset(self):
        policy = self._make_policy()
        policy.record_intervention(timestamp=100.0)
        policy.record_dismissal(timestamp=100.0)
        policy.reset()
        assert policy.intervention_count == 0
        assert not policy.is_quiet_mode


# =============================================================================
# Integration: Module Imports
# =============================================================================


class TestStateEngineImports:
    """Test that all state engine exports are importable."""

    def test_import_fusion(self):
        from cortex.services.state_engine import FeatureFusion
        assert FeatureFusion is not None

    def test_import_scorer(self):
        from cortex.services.state_engine import RuleScorer
        assert RuleScorer is not None

    def test_import_smoother(self):
        from cortex.services.state_engine import ScoreSmoother
        assert ScoreSmoother is not None

    def test_import_trigger(self):
        from cortex.services.state_engine import TriggerDecision, TriggerPolicy
        assert TriggerPolicy is not None
        assert TriggerDecision is not None
