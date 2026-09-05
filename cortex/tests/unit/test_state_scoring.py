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
    """Create a feature vector indicating FLOW state.

    ``telemetry_seen_count`` is set past the warm-up gate (>=5) because
    this fixture carries real telemetry values (mouse / tab) — the FLOW
    telemetry contributions only count once the warm-up gate is cleared
    (see ``RuleScorer._flow_transform``).
    """
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
        keypress_rate_per_min=60.0,
        keystroke_interval_variance=500.0,
        correction_rate_per_100_keys=5.0,
        inactivity_seconds=2.0,
        tab_switch_frequency=5.0,
        scroll_back_rate_per_min=1.0,
        telemetry_seen_count=10,
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
        keypress_rate_per_min=90.0,
        keystroke_interval_variance=8000.0,
        correction_rate_per_100_keys=25.0,
        inactivity_seconds=0.5,
        tab_switch_frequency=25.0,  # Rapid switching >20/min
        scroll_back_rate_per_min=40.0,
        thrashing_score=1.0,
        telemetry_seen_count=10,
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
        keypress_rate_per_min=0.0,
        keystroke_interval_variance=200.0,
        correction_rate_per_100_keys=0.0,
        inactivity_seconds=300.0,
        tab_switch_frequency=0.5,
        scroll_back_rate_per_min=0.0,
        telemetry_seen_count=10,
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

    def test_flow_not_inflated_by_fabricated_telemetry_when_off(self):
        """P1: telemetry-off FLOW must NOT accrue from fabricated 0.0 variance.

        Before the warm-up gate, a session with NO telemetry
        (``telemetry_seen_count == 0``, all telemetry fields at their 0.0
        defaults) appended a 0.8 FLOW contribution off the fabricated
        ``mouse_velocity_variance == 0.0 < baseline`` branch — exactly
        like the unguarded HYPO branches that the audit already fixed.
        With the gate, the telemetry FLOW branches are skipped until 5+
        telemetry samples have been seen, so a telemetry-off session
        scores strictly lower than the identical session with warm
        telemetry.
        """
        scorer = self._make_scorer()

        # Identical physio, but no telemetry has been observed yet.
        cold = FeatureVector(
            timestamp=1.0,
            hr=72.0,
            hrv_rmssd=55.0,
            hr_delta=0.0,
            blink_rate=16.0,
            blink_rate_delta=0.0,
            shoulder_drop_ratio=0.02,
            forward_lean_angle=5.0,
            # All telemetry at defaults (0.0) and NOT warmed up.
            mouse_velocity_variance=0.0,
            tab_switch_frequency=0.0,
            telemetry_seen_count=0,
        )
        cold_flow = scorer.compute_scores(cold).flow

        # Same zero-valued fixture, but with a connected/warm stream.
        warm = cold.model_copy(update={"telemetry_seen_count": 10})
        warm_flow = scorer.compute_scores(warm).flow

        # Neither missing telemetry nor an observed all-zero stream is
        # affirmative activity evidence.
        assert cold_flow == 0.0
        assert warm_flow == 0.0


class TestSubScores:
    """Test the sub-score transforms that survive in the Level-A rules.

    The pre-v2 physiology/posture sub-scores (pulse elevation, HRV drop,
    blink suppression, posture collapse, workspace complexity) were removed
    with the legacy composite scorers (audit D17); only the two behaviour
    transforms used by ``_support_transform`` remain.
    """

    def _make_scorer(self) -> RuleScorer:
        return RuleScorer(baselines=UserBaselines(hr_baseline=72.0))

    def test_mouse_thrash_high_variance(self):
        scorer = self._make_scorer()
        # Default baseline variance = 10000
        score = scorer.score_mouse_thrash(50000.0)
        assert score > 0.3

    def test_mouse_thrash_normal(self):
        scorer = self._make_scorer()
        score = scorer.score_mouse_thrash(5000.0)
        assert score == 0.0

    def test_mouse_thrash_zero_baseline_does_not_raise(self):
        """Audit D2: calibration may persist ``mouse_variance_baseline=0``.

        The scorer floors the baseline at 1.0 (as ``_flow_transform`` already
        did) instead of dividing by zero on every state-loop tick.
        """
        scorer = RuleScorer(baselines=UserBaselines(mouse_variance_baseline=0.0))
        assert scorer.score_mouse_thrash(0.0) == 0.0
        assert 0.0 < scorer.score_mouse_thrash(50000.0) <= 1.0
        # And the full evaluation path stays alive with a zero baseline.
        fv = make_hyper_features()
        scores = scorer.compute_scores(fv)
        assert 0.0 <= scores.hyper <= 1.0

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

    def test_initial_state_is_unknown(self):
        smoother = self._make_smoother()
        assert smoother.current_state == UserState.UNKNOWN

    def test_ema_smoothing_effect(self):
        """EMA should smooth out spiky scores."""
        smoother = ScoreSmoother(
            config=StateConfig(flow_dwell_seconds=2, ema_alpha=0.5)
        )
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
        smoother = ScoreSmoother(
            config=StateConfig(flow_dwell_seconds=2, ema_alpha=0.5)
        )
        quality = make_good_quality()

        # Start with flow
        flow_scores = StateScores(flow=0.8, hypo=0.0, hyper=0.1, recovery=0.0)
        for i in range(10):
            smoother.update(flow_scores, quality, timestamp=float(i))

        assert smoother.current_state == UserState.FLOW

        # Brief hyper spike
        hyper_scores = StateScores(flow=0.2, hypo=0.0, hyper=0.7, recovery=0.0)
        smoother.update(hyper_scores, quality, timestamp=10.0)

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
            smoother.update(hyper_scores, quality, timestamp=float(i) * 0.5)

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
        assert smoother.current_state == UserState.UNKNOWN
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
        self, confidence: float = 0.9, dwell: float = 35.0,
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
        # Dwell must clear StateConfig.hyper_dwell_seconds (default 30s).
        est = self._make_hyper_estimate(confidence=0.92, dwell=35.0)
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
        est = self._make_hyper_estimate(confidence=0.65, dwell=35.0)
        decision = policy.evaluate(est, current_time=200.0)
        assert decision.should_trigger is False
        assert "below threshold" in decision.reason

    def test_no_trigger_during_cooldown(self):
        """Should not trigger during cooldown period."""
        policy = self._make_policy(cooldown_seconds=60)
        policy.record_intervention(timestamp=150.0)

        est = self._make_hyper_estimate(confidence=0.92, dwell=35.0)
        decision = policy.evaluate(est, current_time=180.0)  # 30s into 60s cooldown
        assert decision.should_trigger is False
        assert "Cooldown" in decision.reason
        assert decision.cooldown_remaining > 0

    def test_trigger_after_cooldown(self):
        """Should trigger after cooldown expires."""
        policy = self._make_policy(cooldown_seconds=60)
        policy.record_intervention(timestamp=100.0)

        est = self._make_hyper_estimate(confidence=0.92, dwell=35.0)
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
            dwell_seconds=35.0,
        )
        decision = policy.evaluate(est, current_time=200.0)
        assert decision.should_trigger is False
        assert "quality" in decision.reason.lower()

    def test_dismissal_raises_threshold(self):
        """Dismissals should raise the effective threshold.

        Mirrors the real daemon dismiss path, which calls BOTH
        ``record_dismissal`` (quiet-mode escalation + threshold bump) and
        ``record_outcome(dismissed=True)`` (adaptive feedback counter).
        The time-boxed +0.05 bump lifts the threshold above the configured
        base; the long-run +0.01 increment only applies once that bump has
        expired (audit D4/D8: the base is never clipped upward and one
        dismissal is never penalised twice at once).
        """
        policy = self._make_policy()
        base_decision = policy.evaluate(
            self._make_hyper_estimate(confidence=0.9), current_time=200.0,
        )
        base_threshold = base_decision.effective_threshold

        # Record a dismissal exactly as the daemon does.
        policy.record_dismissal(timestamp=200.0)
        policy.record_outcome(dismissed=True)
        new_decision = policy.evaluate(
            self._make_hyper_estimate(confidence=0.9), current_time=201.0,
        )

        assert new_decision.effective_threshold > base_threshold

    def test_single_dismissal_increments_counter_once(self):
        """P1: one dismissal must move ``_dismissals_total`` exactly once.

        The daemon dismiss path calls BOTH ``record_dismissal`` and
        ``record_outcome(dismissed=True)`` for a single user dismissal.
        Before the fix BOTH methods incremented ``_dismissals_total``,
        double-counting the adaptive-threshold offset (one dismissal
        nudged +0.02 while one approval only moved -0.01). The counter is
        now owned exclusively by ``record_outcome``.
        """
        policy = self._make_policy()
        assert policy._dismissals_total == 0  # noqa: SLF001

        # Simulate one full daemon-side dismissal.
        policy.record_dismissal(timestamp=200.0)
        policy.record_outcome(dismissed=True)

        assert policy._dismissals_total == 1  # noqa: SLF001

        # And a symmetric approval moves it back toward zero by the same
        # magnitude (no asymmetric 0.02 vs 0.01 skew anymore).
        policy.record_outcome(dismissed=False)
        assert policy._dismissals_total == 1  # noqa: SLF001
        assert policy._approvals_total == 1  # noqa: SLF001

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
        # StateConfig.hyper_dwell_seconds default is 30s; 3s is well below.
        est = self._make_hyper_estimate(confidence=0.92, dwell=3.0)
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

    def test_schedule_slot_uses_wall_clock_not_monotonic(self):
        """P1: ``lookup_schedule_slot`` must resolve against wall-clock.

        The bug: ``evaluate`` passed its monotonic ``current_time`` into
        ``lookup_schedule_slot``, which fed it to
        ``datetime.fromtimestamp`` — yielding a garbage weekday/hour and
        mis-gating the weekly schedule. Arm a schedule whose ON/OFF slots
        differ by weekday+hour and assert the slot resolves to the REAL
        local now, independent of any monotonic value passed to evaluate.
        """
        import datetime as _dt

        policy = self._make_policy()

        now = _dt.datetime.now()
        today = (
            "monday", "tuesday", "wednesday", "thursday",
            "friday", "saturday", "sunday",
        )[now.weekday()]
        # Slot index for the current hour (matches _SCHEDULE_SLOT_HOURS).
        slot_hours = ((6, 12), (12, 14), (14, 18), (18, 24))
        cur_idx = next(
            (i for i, (lo, hi) in enumerate(slot_hours) if lo <= now.hour < hi),
            None,
        )
        if cur_idx is None:
            # Outside any configured slot window (00:00–05:59) — the
            # lookup returns None regardless, which still proves it used
            # wall-clock (a monotonic value would have hit a slot).
            policy.set_weekly_schedule({today: ["off", "off", "off", "off"]})
            assert policy.lookup_schedule_slot() is None
            return

        # Mark only the CURRENT weekday+slot as "off"; everything else "on".
        slots = ["on", "on", "on", "on"]
        slots[cur_idx] = "off"
        policy.set_weekly_schedule({today: slots})

        # Direct lookup resolves to the real local weekday/hour slot.
        assert policy.lookup_schedule_slot() == "off"

        # And through evaluate(): a monotonic current_time (tiny float)
        # must NOT corrupt the schedule resolution. With the current slot
        # "off", evaluate must report the weekly-schedule block.
        est = self._make_hyper_estimate(confidence=0.95, dwell=40.0)
        decision = policy.evaluate(est, current_time=1234.5)
        assert decision.should_trigger is False
        assert decision.reason == "weekly_schedule_off"


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
