"""Tests for Phase 4b-retry-2 audit closures.

Covers:
* TASK B — FeatureFusion physio_missing + telemetry_seen_count
* TASK C — RuleScorer forward_lean None handling + cold-start gate
* TASK D — TriggerPolicy HYPER physio/kinematics signal-quality floor
* TASK E — SessionReportGenerator NTP back-jump clamp
* TASK F — Real interventions_triggered / interventions_accepted plumbing
"""
from __future__ import annotations

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
)
from cortex.services.session_report.generator import SessionReportGenerator
from cortex.services.state_engine.feature_fusion import FeatureFusion
from cortex.services.state_engine.rule_scorer import RuleScorer
from cortex.services.state_engine.trigger_policy import TriggerPolicy

# ---------------------------------------------------------------------------
# TASK B — FeatureFusion physio gating + telemetry cold-start
# ---------------------------------------------------------------------------


class TestFeatureFusionPhysioGating:
    def test_physio_missing_true_when_no_physio_channel(self):
        fusion = FeatureFusion()
        kin = KinematicFeatures(
            blink_rate=15.0, confidence=0.7,
        )
        fusion.update_kinematics(kin, timestamp=0.0)
        vec, _ = fusion.fuse(timestamp=0.0)
        assert vec.physio_missing is True

    def test_physio_missing_false_when_physio_valid(self):
        fusion = FeatureFusion()
        physio = PhysioFeatures(
            pulse_bpm=72.0,
            pulse_quality=0.9,
            pulse_variability_proxy=45.0,
            valid=True,
        )
        fusion.update_physio(physio, timestamp=0.0)
        vec, _ = fusion.fuse(timestamp=0.0)
        assert vec.physio_missing is False

    def test_physio_missing_true_when_physio_invalid(self):
        fusion = FeatureFusion()
        physio = PhysioFeatures(pulse_quality=0.0, valid=False)
        fusion.update_physio(physio, timestamp=0.0)
        vec, _ = fusion.fuse(timestamp=0.0)
        assert vec.physio_missing is True

    def test_telemetry_seen_count_increments(self):
        fusion = FeatureFusion()
        assert fusion.fuse(timestamp=0.0)[0].telemetry_seen_count == 0
        tel = TelemetryFeatures(
            mouse_velocity_mean=300.0,
            mouse_velocity_variance=50.0,
            click_frequency=0.5,
            keystroke_interval_variance=100.0,
            window_switch_rate=2.0,
            inactivity_seconds=0.0,
            mouse_jerk_score=0.0,
            click_burst_score=0.0,
            keyboard_burst_score=0.0,
            backspace_density=0.0,
        )
        for i in range(3):
            fusion.update_telemetry(tel, timestamp=float(i))
        assert fusion.fuse(timestamp=3.0)[0].telemetry_seen_count == 3
        for i in range(3, 7):
            fusion.update_telemetry(tel, timestamp=float(i))
        assert fusion.fuse(timestamp=7.0)[0].telemetry_seen_count == 7

    def test_forward_lean_angle_none_when_kinematics_score_none(self):
        fusion = FeatureFusion()
        kin = KinematicFeatures(
            blink_rate=15.0,
            confidence=0.7,
            forward_lean_score=None,
            shoulder_drop_ratio=0.2,
        )
        fusion.update_kinematics(kin, timestamp=0.0)
        vec, _ = fusion.fuse(timestamp=0.0)
        assert vec.forward_lean_angle is None
        assert vec.shoulder_drop_ratio == 0.2


# ---------------------------------------------------------------------------
# TASK C — RuleScorer posture + HYPO cold-start
# ---------------------------------------------------------------------------


def _hypo_fv(*, telemetry_seen: int, forward_lean: float | None = None,
             shoulder_drop: float | None = None) -> FeatureVector:
    """Build a minimal FeatureVector targeted at the HYPO branch."""
    return FeatureVector(
        timestamp=0.0,
        hr=70.0,
        hrv_rmssd=50.0,
        blink_rate=15.0,
        shoulder_drop_ratio=shoulder_drop,
        forward_lean_angle=forward_lean,
        mouse_velocity_mean=0.0,
        mouse_velocity_variance=0.0,
        click_frequency=0.0,
        keystroke_interval_variance=0.0,
        tab_switch_frequency=0.0,
        respiration_rate=15.0,
        telemetry_seen_count=telemetry_seen,
    )


class TestRuleScorerPostureColdStart:
    def test_posture_hypo_skipped_when_forward_lean_none(self):
        scorer = RuleScorer(baselines=UserBaselines())
        fv = _hypo_fv(telemetry_seen=10, forward_lean=None, shoulder_drop=0.5)
        scores = scorer.compute_scores(fv)
        # Posture HYPO contribution should be skipped — the score is
        # driven by HR/blink/screen-apnea only, none of which fire on
        # these neutral values, so HYPO should be close to zero.
        assert scores.hypo < 0.3

    def test_posture_hypo_fires_when_forward_lean_present(self):
        scorer = RuleScorer(baselines=UserBaselines())
        fv = _hypo_fv(telemetry_seen=10, forward_lean=5.0, shoulder_drop=0.3)
        scores = scorer.compute_scores(fv)
        # Slump (lean<15 + drop>0.1) should contribute a HYPO score.
        assert scores.hypo > 0.0

    def test_hypo_telemetry_skipped_during_cold_start(self):
        scorer = RuleScorer(baselines=UserBaselines())
        # Same neutral features but telemetry_seen_count < 5 — the mouse
        # / window-switch HYPO branches must be skipped.
        fv = _hypo_fv(telemetry_seen=0, forward_lean=None, shoulder_drop=None)
        scores = scorer.compute_scores(fv)
        # Without those branches there is no remaining strong HYPO signal,
        # so the score should be close to zero.
        assert scores.hypo == 0.0

    def test_hypo_telemetry_fires_after_warmup(self):
        scorer = RuleScorer(baselines=UserBaselines())
        fv = _hypo_fv(telemetry_seen=5, forward_lean=None, shoulder_drop=None)
        scores = scorer.compute_scores(fv)
        # Mouse drift + low switch rate now contribute → non-zero HYPO.
        assert scores.hypo > 0.0


# ---------------------------------------------------------------------------
# TASK D — TriggerPolicy HYPER SQ floor
# ---------------------------------------------------------------------------


def _hyper_estimate(*, physio: float, kinematics: float, telemetry: float,
                    confidence: float = 0.92, dwell: float = 35.0,
                    ) -> StateEstimate:
    return StateEstimate(
        state="HYPER",
        confidence=confidence,
        scores=StateScores(flow=0.05, hypo=0.0, hyper=confidence, recovery=0.0),
        reasons=["test"],
        signal_quality=SignalQuality(
            physio=physio, kinematics=kinematics, telemetry=telemetry,
        ),
        timestamp=0.0,
        dwell_seconds=dwell,
    )


class TestHyperSignalQualityFloor:
    def test_telemetry_only_blocks_hyper(self):
        """Physio + kinematics near zero must defer the HYPER trigger
        even when telemetry is strong enough that the overall quality
        is ``acceptable``."""
        policy = TriggerPolicy(
            config=InterventionConfig(adaptive_threshold_enabled=False),
            state_config=StateConfig(),
        )
        est = _hyper_estimate(physio=0.1, kinematics=0.1, telemetry=0.9)
        decision = policy.evaluate(est, current_time=200.0)
        assert decision.should_trigger is False
        assert "physio" in decision.reason.lower()

    def test_physio_floor_alone_passes(self):
        """Physio >= 0.3 passes the SQ floor even with weak kinematics."""
        policy = TriggerPolicy(
            config=InterventionConfig(adaptive_threshold_enabled=False),
            state_config=StateConfig(),
        )
        est = _hyper_estimate(physio=0.35, kinematics=0.1, telemetry=0.9)
        decision = policy.evaluate(est, current_time=200.0)
        assert decision.should_trigger is True

    def test_kinematics_floor_alone_passes(self):
        """Kinematics >= 0.5 passes the SQ floor even with no physio."""
        policy = TriggerPolicy(
            config=InterventionConfig(adaptive_threshold_enabled=False),
            state_config=StateConfig(),
        )
        est = _hyper_estimate(physio=0.0, kinematics=0.6, telemetry=0.9)
        decision = policy.evaluate(est, current_time=200.0)
        assert decision.should_trigger is True


# ---------------------------------------------------------------------------
# TASK E — SessionReportGenerator NTP backjump
# ---------------------------------------------------------------------------


class TestSessionReportNTPBackjump:
    def test_negative_dt_clamps_to_zero(self, caplog):
        gen = SessionReportGenerator()
        gen.start()
        gen.record_state("FLOW", timestamp=1000.0)
        # Simulate NTP back-jumping 10s.
        with caplog.at_level("WARNING"):
            gen.record_state("HYPER", timestamp=990.0)
        # FLOW duration must be clamped to 0, not -10.
        report = gen.finish(end_timestamp=1500.0)
        assert report.time_in_flow_seconds >= 0.0
        # Warning was emitted.
        assert any(
            "Negative state duration" in rec.message for rec in caplog.records
        )

    def test_intervention_counters_default_zero(self):
        gen = SessionReportGenerator()
        gen.start()
        gen.record_state("FLOW", timestamp=0.0)
        report = gen.finish(end_timestamp=100.0)
        assert report.interventions_triggered == 0
        assert report.interventions_accepted == 0

    def test_intervention_counters_diverge(self):
        """interventions_triggered should be able to exceed
        interventions_accepted — that's the normal case where the
        user dismissed or did not rate some plans."""
        gen = SessionReportGenerator()
        gen.start()
        gen.increment_interventions_triggered(5)
        gen.increment_interventions_accepted(2)
        report = gen.finish(end_timestamp=100.0)
        assert report.interventions_triggered == 5
        assert report.interventions_accepted == 2
        # Sanity: divergence is preserved through (de)serialization.
        roundtrip = type(report).model_validate_json(report.model_dump_json())
        assert roundtrip.interventions_triggered == 5
        assert roundtrip.interventions_accepted == 2
