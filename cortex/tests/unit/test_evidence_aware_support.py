"""Adversarial contract tests for evidence-aware support inference."""

from __future__ import annotations

from itertools import product
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from cortex.libs.config.settings import StateConfig
from cortex.libs.schemas.features import (
    FeatureName,
    FeatureValue,
    FeatureVector,
    TelemetryFeatures,
)
from cortex.libs.schemas.observations import MissingReason
from cortex.libs.schemas.realtime import StateUpdatePayload
from cortex.libs.schemas.state import (
    EstimateStatus,
    InferenceModelIdentity,
    SignalQuality,
    StateEstimate,
    StateScores,
    SupportScores,
    UserState,
)
from cortex.services.state_engine.evaluation_protocol import (
    LabeledEpisode,
    StudyExclusion,
    SupportOutcome,
    build_participant_held_out_folds,
    split_development_and_calibration,
)
from cortex.services.state_engine.feature_fusion import FeatureFusion
from cortex.services.state_engine.feature_schema import (
    FEATURE_DEFINITIONS,
    ORDERED_FEATURES,
    feature_schema_sha256,
    to_ordered_array,
)
from cortex.services.state_engine.focus_break_policy import FocusBreakPolicy
from cortex.services.state_engine.model_registry import (
    RegisteredModel,
    SupportModelRegistry,
)
from cortex.services.state_engine.rule_scorer import RuleScorer
from cortex.services.state_engine.smoother import ScoreSmoother
from cortex.services.state_engine.support_inference import SupportInferenceEngine
from cortex.services.telemetry_engine.feature_aggregator import FeatureAggregator


def _valid(name: FeatureName, value: float, quality: float = 1.0) -> FeatureValue:
    definition = FEATURE_DEFINITIONS[name]
    return FeatureValue(
        value=value,
        valid=True,
        quality=quality,
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


def _vector(values: dict[FeatureName, float], *, quality: float = 1.0) -> FeatureVector:
    return FeatureVector(
        timestamp=1.0,
        telemetry_seen_count=5,
        features={
            name: _valid(name, values[name], quality) if name in values else _missing(name)
            for name in FeatureName
        },
    )


_SUPPORT_VALUES = {
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

_FLOW_VALUES = {
    FeatureName.MOUSE_VELOCITY_MEAN: 400.0,
    FeatureName.MOUSE_VELOCITY_VARIANCE: 5_000.0,
    FeatureName.CLICK_FREQUENCY: 0.5,
    FeatureName.KEYPRESS_RATE_PER_MIN: 60.0,
    FeatureName.KEYSTROKE_INTERVAL_VARIANCE: 500.0,
    FeatureName.CORRECTION_RATE_PER_100_KEYS: 5.0,
    FeatureName.INACTIVITY_SECONDS: 2.0,
    FeatureName.TAB_SWITCH_RATE_PER_MIN: 3.0,
    FeatureName.SCROLL_BACK_RATE_PER_MIN: 1.0,
    FeatureName.THRASHING_SCORE: 0.0,
}


def test_feature_value_requires_coherent_missingness_and_is_frozen() -> None:
    with pytest.raises(ValidationError):
        FeatureValue(
            value=1.0,
            valid=False,
            source_window_ms=1,
            algorithm_version="test",
            missing_reason=MissingReason.SOURCE_DISCONNECTED,
        )
    value = _valid(FeatureName.CLICK_FREQUENCY, 0.0)
    with pytest.raises(ValidationError):
        value.quality = 0.5


def test_feature_catalog_is_complete_ordered_and_dimension_strict() -> None:
    assert {item.name for item in ORDERED_FEATURES} == set(FeatureName)
    assert len(feature_schema_sha256()) == 64
    vector = _vector(_FLOW_VALUES)
    values, mask = to_ordered_array(vector, expected_dimension=len(ORDERED_FEATURES))
    assert len(values) == len(mask) == len(ORDERED_FEATURES)
    assert mask[0] is False
    with pytest.raises(ValueError, match="model expects"):
        to_ordered_array(vector, expected_dimension=len(ORDERED_FEATURES) - 1)


def _telemetry(**updates: object) -> TelemetryFeatures:
    data: dict[str, object] = {
        "mouse_velocity_mean": 400.0,
        "mouse_velocity_variance": 5_000.0,
        "mouse_jerk_score": 0.0,
        "click_burst_score": 0.0,
        "click_frequency": 0.5,
        "keypress_rate_per_min": 60.0,
        "keyboard_burst_score": 0.0,
        "keystroke_interval_variance": 500.0,
        "backspace_density": 0.05,
        "correction_rate_per_100_keys": 5.0,
        "inactivity_seconds": 2.0,
        "window_switch_rate": 3.0,
        "scroll_back_rate_per_min": 1.0,
        "observation_window_seconds": 15.0,
        "mouse_move_count": 50,
        "click_press_count": 8,
        "key_press_count": 30,
        "scroll_event_count": 10,
        "window_focus_event_count": 2,
        "window_focus_source_available": True,
    }
    data.update(updates)
    return TelemetryFeatures(**data)


def test_fusion_requires_metric_exposure_but_preserves_observed_zero() -> None:
    legacy = FeatureFusion()
    legacy.update_telemetry(
        _telemetry(
            observation_window_seconds=None,
            mouse_move_count=None,
            key_press_count=None,
        ),
        timestamp=1.0,
    )
    legacy_vector, _ = legacy.fuse(timestamp=1.0)
    assert not legacy_vector.features[FeatureName.CLICK_FREQUENCY].valid

    observed = FeatureFusion()
    observed.update_telemetry(
        _telemetry(
            click_frequency=0.0,
            click_press_count=0,
            keypress_rate_per_min=0.0,
            key_press_count=0,
        ),
        timestamp=1.0,
    )
    vector, _ = observed.fuse(timestamp=1.0)
    click = vector.features[FeatureName.CLICK_FREQUENCY]
    assert click.valid and click.value == 0.0
    assert not vector.features[FeatureName.KEYSTROKE_INTERVAL_VARIANCE].valid

    unavailable = FeatureFusion()
    unavailable.update_telemetry(
        _telemetry(window_focus_source_available=False), timestamp=1.0
    )
    unavailable_vector, _ = unavailable.fuse(timestamp=1.0)
    assert not unavailable_vector.features[
        FeatureName.TAB_SWITCH_RATE_PER_MIN
    ].valid


def test_no_event_inactivity_is_bounded_by_collector_exposure() -> None:
    hooks = Mock()
    hooks.get_events_in_window.return_value = {
        "mouse_moves": [],
        "mouse_clicks": [],
        "mouse_scrolls": [],
        "key_events": [],
    }
    aggregator = FeatureAggregator(hooks)
    first = aggregator.build_features(window_seconds=15.0, current_time=1_000_000.0)
    second = aggregator.build_features(window_seconds=15.0, current_time=1_000_010.0)
    capped = aggregator.build_features(window_seconds=15.0, current_time=1_000_100.0)
    assert first.inactivity_seconds == 0.0
    assert second.inactivity_seconds == 10.0
    assert capped.inactivity_seconds == 15.0


def test_camera_presence_cannot_change_behavior_support_scores() -> None:
    scorer = RuleScorer()
    without_camera = scorer.evaluate(_vector(_SUPPORT_VALUES))
    with_camera = scorer.evaluate(_vector({
        **_SUPPORT_VALUES,
        FeatureName.HEART_RATE_BPM: 180.0,
        FeatureName.BLINK_RATE_PER_MIN: 0.0,
        FeatureName.HEAD_NECK_FLEXION_SCORE: 1.0,
    }))
    assert with_camera.scores == without_camera.scores
    assert with_camera.evidence_coverage == without_camera.evidence_coverage


def test_all_channel_presence_combinations_fail_closed() -> None:
    scorer = RuleScorer()
    camera_values = {
        FeatureName.HEART_RATE_BPM: 180.0,
        FeatureName.BLINK_RATE_PER_MIN: 0.0,
        FeatureName.HEAD_NECK_FLEXION_SCORE: 1.0,
    }
    behavior_values = {
        key: value
        for key, value in _SUPPORT_VALUES.items()
        if key != FeatureName.THRASHING_SCORE
    }
    for camera_on, behavior_on, graph_on in product((False, True), repeat=3):
        values: dict[FeatureName, float] = {}
        if camera_on:
            values.update(camera_values)
        if behavior_on:
            values.update(behavior_values)
        if graph_on:
            values[FeatureName.THRASHING_SCORE] = 1.0
        evaluation = scorer.evaluate(_vector(values))
        if not behavior_on:
            assert evaluation.status == EstimateStatus.INSUFFICIENT_EVIDENCE
            assert evaluation.scores.support_likely == 0.0
        else:
            assert evaluation.scores.support_likely > 0.0


def test_removing_or_lowering_evidence_never_increases_support_certainty() -> None:
    scorer = RuleScorer()
    vector = _vector(_SUPPORT_VALUES)
    baseline = scorer.evaluate(vector)
    support_features = {
        FeatureName.MOUSE_VELOCITY_VARIANCE,
        FeatureName.CLICK_FREQUENCY,
        FeatureName.KEYSTROKE_INTERVAL_VARIANCE,
        FeatureName.CORRECTION_RATE_PER_100_KEYS,
        FeatureName.TAB_SWITCH_RATE_PER_MIN,
        FeatureName.SCROLL_BACK_RATE_PER_MIN,
        FeatureName.THRASHING_SCORE,
    }
    for name in support_features:
        removed = vector.model_copy(deep=True)
        removed.features[name] = _missing(name)
        removed_eval = scorer.evaluate(removed)
        assert removed_eval.scores.support_likely <= baseline.scores.support_likely
        assert removed_eval.evidence_coverage <= baseline.evidence_coverage

        lowered = vector.model_copy(deep=True)
        lowered.features[name] = _valid(name, _SUPPORT_VALUES[name], quality=0.2)
        lowered_eval = scorer.evaluate(lowered)
        assert lowered_eval.scores.support_likely <= baseline.scores.support_likely
        assert lowered_eval.evidence_coverage <= baseline.evidence_coverage


def test_zero_stream_is_not_flow_and_quiet_requires_inactivity_corroboration() -> None:
    scorer = RuleScorer()
    zero_values = dict.fromkeys((FeatureName.MOUSE_VELOCITY_MEAN, FeatureName.MOUSE_VELOCITY_VARIANCE, FeatureName.CLICK_FREQUENCY, FeatureName.KEYPRESS_RATE_PER_MIN, FeatureName.KEYSTROKE_INTERVAL_VARIANCE, FeatureName.CORRECTION_RATE_PER_100_KEYS, FeatureName.INACTIVITY_SECONDS, FeatureName.TAB_SWITCH_RATE_PER_MIN, FeatureName.SCROLL_BACK_RATE_PER_MIN, FeatureName.THRASHING_SCORE), 0.0)
    assert scorer.evaluate(_vector(zero_values)).scores.flow_like == 0.0

    inactivity_only = scorer.evaluate(_vector({
        FeatureName.INACTIVITY_SECONDS: 300.0,
    }))
    assert inactivity_only.scores.under_engaged == 0.0
    corroborated = scorer.evaluate(_vector({
        FeatureName.INACTIVITY_SECONDS: 300.0,
        FeatureName.KEYPRESS_RATE_PER_MIN: 0.0,
    }))
    assert corroborated.scores.under_engaged > 0.0


def test_smoother_unknown_dwell_monotonic_replay_and_temporal_recovery() -> None:
    config = StateConfig(
        ema_alpha=1.0,
        estimate_entry_threshold=0.3,
        estimate_exit_threshold=0.2,
        hyper_dwell_seconds=1,
        flow_dwell_seconds=1,
        hypo_dwell_seconds=1,
    )
    quality = SignalQuality(telemetry=1.0)
    scorer = RuleScorer(config=config)
    flow = scorer.evaluate(_vector(_FLOW_VALUES))
    support = scorer.evaluate(_vector(_SUPPORT_VALUES))
    assert flow.status == support.status == EstimateStatus.ESTIMATED

    first = ScoreSmoother(config)
    assert first.current_state == UserState.UNKNOWN
    assert first.update(flow, quality, timestamp=0.0).status == "warming_up"
    flow_estimate = first.update(flow, quality, timestamp=1.0)
    assert flow_estimate.state == "FLOW"
    before_regression = flow_estimate.dwell_seconds
    regressed = first.update(flow, quality, timestamp=0.5)
    assert regressed.dwell_seconds >= before_regression

    # A deterministic replay produces the same domain output (UUID excluded).
    second = ScoreSmoother(config)
    replay = [
        second.update(flow, quality, timestamp=0.0),
        second.update(flow, quality, timestamp=1.0),
        second.update(flow, quality, timestamp=0.5),
    ]
    assert [
        (item.state, item.status, item.scores, item.dwell_seconds)
        for item in replay
    ][-1] == (
        regressed.state,
        regressed.status,
        regressed.scores,
        regressed.dwell_seconds,
    )

    recovery = ScoreSmoother(config)
    recovery.update(support, quality, timestamp=0.0)
    assert recovery.update(support, quality, timestamp=1.0).state == "HYPER"
    recovery.update(flow, quality, timestamp=2.0)
    recovered = recovery.update(flow, quality, timestamp=7.0)
    assert recovered.state == "RECOVERY"

    missing = scorer.evaluate(_vector({}))
    assert first.update(missing, quality, timestamp=8.0).state == "UNKNOWN"


def test_probability_fields_require_separate_calibration_artifact() -> None:
    estimate_args = {
        "state": "FLOW",
        "confidence": 0.7,
        "scores": StateScores(flow=0.7),
        "probabilities": SupportScores(flow_like=0.7),
        "reasons": [],
        "signal_quality": SignalQuality(telemetry=1.0),
        "timestamp": 1.0,
    }
    with pytest.raises(ValidationError, match="calibration artifact"):
        StateEstimate(**estimate_args)

    identity = InferenceModelIdentity(
        name="future-model",
        version="0.1.0",
        feature_schema_version="test",
        validation_status="research_only",
    )
    with pytest.raises(ValueError, match="calibration artifact"):
        RegisteredModel(
            identity=identity,
            kind="probabilistic",
            model_card="docs/model-cards/future.md",
            production_eligible=False,
        )


def test_state_update_rejects_probability_without_artifact() -> None:
    with pytest.raises(ValidationError, match="calibration artifact"):
        StateUpdatePayload(
            state="FLOW",
            confidence=0.7,
            scores=StateScores(flow=0.7),
            probabilities=SupportScores(flow_like=0.7),
            signal_quality=SignalQuality(telemetry=1.0),
        )


def test_model_registry_safe_null_is_wired_and_fail_closed() -> None:
    registry = SupportModelRegistry()
    registry.rollback_to_safe_null()
    evaluation = SupportInferenceEngine(RuleScorer(), registry).evaluate(
        _vector(_SUPPORT_VALUES)
    )
    assert evaluation.status == EstimateStatus.INSUFFICIENT_EVIDENCE
    assert evaluation.scores == SupportScores()
    assert evaluation.model.name == "no-inference"


def test_focus_break_policy_is_opt_in_monotonic_and_one_shot() -> None:
    disabled = FocusBreakPolicy(interval_minutes=0.1, enabled=False)
    disabled.evaluate(active=True, timestamp=0.0)
    assert disabled.evaluate(active=True, timestamp=10.0).status == "disabled"

    policy = FocusBreakPolicy(interval_minutes=0.1, enabled=True)
    assert not policy.evaluate(active=True, timestamp=0.0).should_recommend
    assert not policy.evaluate(active=True, timestamp=3.0).should_recommend
    due = policy.evaluate(active=True, timestamp=6.0)
    assert due.should_recommend and due.active_elapsed_seconds == 6.0
    assert policy.evaluate(active=True, timestamp=7.0).status == "already_recommended"
    policy.snooze(0.1, timestamp=7.0)
    assert policy.evaluate(active=True, timestamp=8.0).status == "snoozed"
    assert policy.evaluate(active=True, timestamp=13.0).should_recommend
    policy.record_break_taken()
    assert policy.active_elapsed_seconds == 0.0

    # Suspend-sized gaps and inactive intervals never count as active work.
    gaps = FocusBreakPolicy(interval_minutes=1.0, enabled=True)
    gaps.evaluate(active=True, timestamp=0.0)
    gaps.evaluate(active=True, timestamp=40.0)
    gaps.evaluate(active=False, timestamp=50.0)
    assert gaps.active_elapsed_seconds == 0.0


def _episodes() -> list[LabeledEpisode]:
    episodes = [
        LabeledEpisode(
            participant_id=f"p{participant}",
            episode_id=f"p{participant}-e{episode}",
            outcome=(
                SupportOutcome.SUPPORT_HELPFUL
                if episode % 2
                else SupportOutcome.SUPPORT_NOT_HELPFUL
            ),
            label_source="post-episode self-report",
        )
        for participant in range(1, 7)
        for episode in range(1, 4)
    ]
    episodes.append(LabeledEpisode(
        participant_id="p7",
        episode_id="excluded",
        outcome=SupportOutcome.UNCERTAIN,
        label_source="post-episode self-report",
        exclusion=StudyExclusion.PROTOCOL_DEVIATION,
    ))
    return episodes


def test_participant_held_out_and_calibration_splits_have_no_leakage() -> None:
    folds = build_participant_held_out_folds(_episodes(), n_splits=3)
    test_participants = [p for fold in folds for p in fold.test_participant_ids]
    assert set(test_participants) == {f"p{index}" for index in range(1, 7)}
    assert len(test_participants) == len(set(test_participants))
    for fold in folds:
        assert not set(fold.train_participant_ids) & set(fold.test_participant_ids)
        assert not set(fold.train_episode_ids) & set(fold.test_episode_ids)

    split = split_development_and_calibration(_episodes())
    assert not set(split.development_participant_ids) & set(
        split.calibration_participant_ids
    )
    assert not set(split.development_episode_ids) & set(split.calibration_episode_ids)
