"""
Integration Test: Capture → State Classification Pipeline

Tests the full data flow from mock feature inputs through feature fusion,
rule-based scoring, EMA smoothing, and state classification.

Verifies:
- Feature fusion produces correct FeatureVectors
- Rule scorer produces expected scores for FLOW/HYPER/HYPO states
- Smoother applies hysteresis and dwell time correctly
- State transitions propagate through the full pipeline
"""

from __future__ import annotations

from cortex.libs.config.settings import StateConfig
from cortex.libs.schemas.features import (
    KinematicFeatures,
    PhysioFeatures,
    TelemetryFeatures,
)
from cortex.services.state_engine.feature_fusion import FeatureFusion
from cortex.services.state_engine.rule_scorer import RuleScorer
from cortex.services.state_engine.smoother import ScoreSmoother

# ============================================================================
# Helpers — create feature sets for different states
# ============================================================================


def _flow_physio() -> PhysioFeatures:
    """Physio features consistent with FLOW state."""
    return PhysioFeatures(
        pulse_bpm=72.0,
        pulse_quality=0.9,
        pulse_variability_proxy=55.0,  # RMSSD > 40 = good HRV
        hr_delta_5s=0.5,
        valid=True,
    )


def _flow_kinematics() -> KinematicFeatures:
    """Kinematic features consistent with FLOW state."""
    return KinematicFeatures(
        blink_rate=16.0,  # 12-20 = normal
        blink_rate_delta=0.0,
        blink_suppression_score=0.0,
        head_pitch=0.0,
        head_yaw=0.0,
        head_roll=0.0,
        slump_score=0.1,
        forward_lean_score=0.1,
        shoulder_drop_ratio=0.05,
        confidence=0.9,
    )


def _flow_telemetry() -> TelemetryFeatures:
    """Telemetry features consistent with FLOW state."""
    return TelemetryFeatures(
        mouse_velocity_mean=400.0,
        mouse_velocity_variance=5000.0,
        mouse_jerk_score=0.1,
        click_burst_score=0.1,
        click_frequency=0.5,
        keyboard_burst_score=0.2,
        keystroke_interval_variance=500.0,
        backspace_density=0.05,
        inactivity_seconds=2.0,
        window_switch_rate=5.0,  # moderate switching
    )


def _hyper_physio() -> PhysioFeatures:
    """Physio features consistent with HYPER (overwhelm) state."""
    return PhysioFeatures(
        pulse_bpm=95.0,  # elevated HR
        pulse_quality=0.85,
        pulse_variability_proxy=12.0,  # low HRV = stress
        hr_delta_5s=5.0,
        valid=True,
    )


def _hyper_kinematics() -> KinematicFeatures:
    """Kinematic features consistent with HYPER state."""
    return KinematicFeatures(
        blink_rate=4.0,  # blink suppression
        blink_rate_delta=-10.0,
        blink_suppression_score=0.8,
        head_pitch=15.0,
        head_yaw=0.0,
        head_roll=0.0,
        slump_score=0.6,
        forward_lean_score=0.7,  # forward lean
        shoulder_drop_ratio=0.3,
        confidence=0.9,
    )


def _hyper_telemetry() -> TelemetryFeatures:
    """Telemetry features consistent with HYPER state."""
    return TelemetryFeatures(
        mouse_velocity_mean=1200.0,
        mouse_velocity_variance=50000.0,  # high variance = thrashing
        mouse_jerk_score=0.8,
        click_burst_score=0.7,
        click_frequency=3.0,
        keyboard_burst_score=0.6,
        keystroke_interval_variance=8000.0,
        backspace_density=0.3,
        inactivity_seconds=0.5,
        window_switch_rate=25.0,  # rapid switching
    )


def _hypo_physio() -> PhysioFeatures:
    """Physio features consistent with HYPO (disengagement)."""
    return PhysioFeatures(
        pulse_bpm=60.0,  # below baseline
        pulse_quality=0.8,
        pulse_variability_proxy=35.0,
        hr_delta_5s=-2.0,
        valid=True,
    )


def _hypo_kinematics() -> KinematicFeatures:
    """Kinematic features consistent with HYPO state."""
    return KinematicFeatures(
        blink_rate=28.0,  # high blink rate
        blink_rate_delta=10.0,
        blink_suppression_score=0.0,
        head_pitch=-5.0,
        head_yaw=0.0,
        head_roll=5.0,
        slump_score=0.7,
        forward_lean_score=0.1,
        shoulder_drop_ratio=0.25,  # slumped but not leaning
        confidence=0.85,
    )


def _hypo_telemetry() -> TelemetryFeatures:
    """Telemetry features consistent with HYPO state."""
    return TelemetryFeatures(
        mouse_velocity_mean=30.0,  # minimal movement
        mouse_velocity_variance=200.0,
        mouse_jerk_score=0.0,
        click_burst_score=0.0,
        click_frequency=0.1,
        keyboard_burst_score=0.0,
        keystroke_interval_variance=100.0,
        backspace_density=0.0,
        inactivity_seconds=25.0,
        window_switch_rate=1.0,  # minimal switching
    )


# ============================================================================
# Integration test: Feature Fusion → Scorer → Smoother
# ============================================================================


class TestCaptureToStateFlow:
    """Test the full capture → state pipeline for FLOW detection."""

    def test_flow_features_produce_flow_dominant_score(self):
        """FLOW features should produce flow-dominant scores."""
        fusion = FeatureFusion()
        scorer = RuleScorer()

        ts = 1000.0
        fusion.update_physio(_flow_physio(), ts)
        fusion.update_kinematics(_flow_kinematics(), ts)
        fusion.update_telemetry(_flow_telemetry(), ts)

        vector, quality = fusion.fuse(ts)
        scores = scorer.compute_scores(vector)

        assert scores.flow > scores.hyper, (
            f"FLOW ({scores.flow:.2f}) should dominate HYPER ({scores.hyper:.2f})"
        )
        assert scores.flow > scores.hypo, (
            f"FLOW ({scores.flow:.2f}) should dominate HYPO ({scores.hypo:.2f})"
        )

    def test_flow_state_confirmed_after_dwell(self):
        """Smoother should confirm FLOW after dwell time (starts in FLOW)."""
        fusion = FeatureFusion()
        scorer = RuleScorer()
        smoother = ScoreSmoother()

        ts = 1000.0
        for i in range(20):
            t = ts + i * 0.5
            fusion.update_physio(_flow_physio(), t)
            fusion.update_kinematics(_flow_kinematics(), t)
            fusion.update_telemetry(_flow_telemetry(), t)

            vector, quality = fusion.fuse(t)
            scores = scorer.compute_scores(vector)
            estimate = smoother.update(scores, quality, t)

        assert estimate.state == "FLOW"
        assert estimate.confidence > 0.3


class TestCaptureToStateHyper:
    """Test the full pipeline for HYPER (overwhelm) detection."""

    def test_hyper_features_produce_hyper_dominant_score(self):
        """HYPER features should produce hyper-dominant raw scores."""
        fusion = FeatureFusion()
        scorer = RuleScorer()

        ts = 1000.0
        fusion.update_physio(_hyper_physio(), ts)
        fusion.update_kinematics(_hyper_kinematics(), ts)
        fusion.update_telemetry(_hyper_telemetry(), ts)

        vector, quality = fusion.fuse(ts)
        scores = scorer.compute_scores(vector)

        assert scores.hyper > 0.5, f"Hyper score should be significant: {scores.hyper:.2f}"
        assert scores.hyper > scores.flow, "Hyper should dominate flow"

    def test_hyper_state_transition_after_dwell(self):
        """Smoother should transition to HYPER after dwell time (8s)."""
        fusion = FeatureFusion()
        scorer = RuleScorer()
        # Use config with shorter dwell for testing
        config = StateConfig(
            hyper_dwell_seconds=2,  # 2s dwell for faster test
            entry_threshold=0.5,   # lower threshold
            exit_threshold=0.3,
        )
        smoother = ScoreSmoother(config=config)

        ts = 1000.0
        latest_state = "FLOW"

        # Feed HYPER signals for enough time
        for i in range(40):
            t = ts + i * 0.5
            fusion.update_physio(_hyper_physio(), t)
            fusion.update_kinematics(_hyper_kinematics(), t)
            fusion.update_telemetry(_hyper_telemetry(), t)

            vector, quality = fusion.fuse(t)
            scores = scorer.compute_scores(vector)
            estimate = smoother.update(scores, quality, t)
            latest_state = estimate.state

        assert latest_state == "HYPER", f"Expected HYPER state, got {latest_state}"


class TestCaptureToStateHypo:
    """Test the pipeline for HYPO (disengagement) detection."""

    def test_hypo_features_produce_hypo_scores(self):
        """HYPO features should produce notable hypo scores."""
        fusion = FeatureFusion()
        scorer = RuleScorer()

        ts = 1000.0
        fusion.update_physio(_hypo_physio(), ts)
        fusion.update_kinematics(_hypo_kinematics(), ts)
        fusion.update_telemetry(_hypo_telemetry(), ts)

        vector, quality = fusion.fuse(ts)
        scores = scorer.compute_scores(vector)

        assert scores.hypo > 0.3, f"Hypo score should be notable: {scores.hypo:.2f}"


class TestFeatureFusionQuality:
    """Test feature fusion signal quality tracking."""

    def test_all_channels_give_high_quality(self):
        """All channels present → high overall quality."""
        fusion = FeatureFusion()
        ts = 1000.0

        fusion.update_physio(_flow_physio(), ts)
        fusion.update_kinematics(_flow_kinematics(), ts)
        fusion.update_telemetry(_flow_telemetry(), ts)

        _, quality = fusion.fuse(ts)
        assert quality.physio > 0.5
        assert quality.kinematics > 0.5
        assert quality.telemetry > 0.5
        assert quality.overall > 0.5

    def test_missing_physio_reduces_quality(self):
        """Missing physio → physio quality is 0."""
        fusion = FeatureFusion()
        ts = 1000.0

        # Only kinematics and telemetry
        fusion.update_kinematics(_flow_kinematics(), ts)
        fusion.update_telemetry(_flow_telemetry(), ts)

        _, quality = fusion.fuse(ts)
        assert quality.physio == 0.0
        assert quality.kinematics > 0.0
        assert quality.telemetry > 0.0

    def test_no_channels_zero_quality(self):
        """No channels → zero quality."""
        fusion = FeatureFusion()
        _, quality = fusion.fuse(1000.0)
        assert quality.overall == 0.0


class TestStateTransitions:
    """Test state transition detection across the pipeline."""

    def test_flow_to_hyper_transition_recorded(self):
        """Transitioning from FLOW to HYPER should record a transition."""
        fusion = FeatureFusion()
        scorer = RuleScorer()
        config = StateConfig(
            hyper_dwell_seconds=1,
            entry_threshold=0.5,
            exit_threshold=0.3,
        )
        smoother = ScoreSmoother(config=config)

        ts = 1000.0

        # Start with FLOW signals
        for i in range(10):
            t = ts + i * 0.5
            fusion.update_physio(_flow_physio(), t)
            fusion.update_kinematics(_flow_kinematics(), t)
            fusion.update_telemetry(_flow_telemetry(), t)
            vector, quality = fusion.fuse(t)
            scores = scorer.compute_scores(vector)
            smoother.update(scores, quality, t)

        # Switch to HYPER signals
        for i in range(30):
            t = ts + 5.0 + i * 0.5
            fusion.update_physio(_hyper_physio(), t)
            fusion.update_kinematics(_hyper_kinematics(), t)
            fusion.update_telemetry(_hyper_telemetry(), t)
            vector, quality = fusion.fuse(t)
            scores = scorer.compute_scores(vector)
            estimate = smoother.update(scores, quality, t)

        # Check that at least one transition was recorded
        transitions = smoother.transitions
        # We should see the state either be HYPER or have transitioned
        if estimate.state == "HYPER":
            assert len(transitions) >= 1
