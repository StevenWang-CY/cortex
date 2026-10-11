"""Numerical and clock-domain invariants of the shipped support pipeline."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from cortex.libs.config.settings import StateConfig
from cortex.libs.schemas.features import FeatureValue, TelemetryFeatures
from cortex.libs.schemas.state import SignalQuality, StateScores
from cortex.libs.schemas.temporal import EventTime
from cortex.services.state_engine.feature_fusion import FeatureFusion
from cortex.services.state_engine.smoother import ScoreSmoother


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_valid_feature_must_be_finite(value: float) -> None:
    with pytest.raises(ValidationError):
        FeatureValue(value=value, valid=True, quality=1.0,
                     source_window_ms=15000, algorithm_version="test")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_fusion_abstains_on_nonfinite_measurement(value: float) -> None:
    feature = FeatureFusion._feature_value(
        value, source_present=True, quality=1.0, age_ms=0,
        source_window_ms=15000, algorithm_version="test",
    )
    assert not feature.valid
    assert feature.value is None
    assert feature.missing_reason == "ARTIFACT"


@pytest.mark.parametrize("field,value", [
    ("ema_alpha", 0), ("ema_alpha", -0.1), ("ema_alpha", 1.1),
    ("ema_alpha", float("nan")), ("ema_alpha", float("inf")),
    ("hyper_dwell_seconds", -1), ("hypo_dwell_seconds", -1),
    ("flow_dwell_seconds", -1),
])
def test_invalid_temporal_configuration_is_rejected(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        StateConfig.model_validate({field: value})


@pytest.mark.parametrize("step", [0.01, 0.1, 0.25, 0.5, 1.0, 2.0])
def test_smoothing_depends_on_elapsed_time_not_delivery_frequency(step: float) -> None:
    smoother = ScoreSmoother()
    quality = SignalQuality(telemetry=1.0)
    # One initial nominal update, then exactly two observed seconds.
    for index in range(round(2 / step) + 1):
        result = smoother.update(StateScores(flow=1.0), quality, timestamp=index * step)
    assert result.scores.flow == pytest.approx(1 - 0.7**5, abs=1e-12)


@pytest.mark.parametrize("timestamp", [float("nan"), float("inf"), -float("inf")])
def test_bad_timestamp_cannot_poison_subsequent_inference(timestamp: float) -> None:
    smoother = ScoreSmoother(StateConfig(ema_alpha=1.0, flow_dwell_seconds=1))
    scores, quality = StateScores(flow=1.0), SignalQuality(telemetry=1.0)
    smoother.update(scores, quality, timestamp=0.0)
    with pytest.raises(ValueError, match="finite"):
        smoother.update(scores, quality, timestamp=timestamp)
    assert smoother.update(scores, quality, timestamp=1.0).state == "FLOW"


def test_new_clock_domain_requires_new_dwell_even_if_timestamp_increases() -> None:
    smoother = ScoreSmoother(StateConfig(ema_alpha=1.0, flow_dwell_seconds=1))
    scores, quality = StateScores(flow=1.0), SignalQuality(telemetry=1.0)
    first_boot, second_boot = uuid4(), uuid4()

    def at(second: int, boot_id):
        return EventTime(observed_at_unix_ms=10000 + second * 1000,
                         observed_at_mono_ns=second * 1_000_000_000, boot_id=boot_id)

    smoother.update(scores, quality, event_time=at(1, first_boot))
    assert smoother.update(scores, quality, event_time=at(2, first_boot)).state == "FLOW"
    restarted = smoother.update(scores, quality, event_time=at(3, second_boot))
    assert restarted.state == "UNKNOWN"
    assert restarted.dwell_seconds == 0
    assert smoother.update(scores, quality, event_time=at(4, second_boot)).state == "FLOW"


def test_telemetry_replays_do_not_complete_warmup_or_replace_current_sample() -> None:
    fusion = FeatureFusion()
    sample = TelemetryFeatures(
        mouse_source_available=True, mouse_velocity_mean=150,
        mouse_velocity_variance=1000, mouse_jerk_score=0, click_burst_score=0, click_frequency=0,
        keyboard_burst_score=0, keystroke_interval_variance=0,
        backspace_density=0, inactivity_seconds=0, window_switch_rate=0,
    )
    fusion.update_telemetry(sample, timestamp=1.0)
    for _ in range(10):
        fusion.update_telemetry(sample.model_copy(update={"mouse_velocity_mean": 999}), timestamp=1.0)
        fusion.update_telemetry(sample, timestamp=0.5)
    vector, _ = fusion.fuse(timestamp=1.0)
    assert vector.telemetry_seen_count == 1
    assert vector.mouse_velocity_mean == 150
    fusion.update_telemetry(sample, timestamp=5.0)
    vector, _ = fusion.fuse(timestamp=5.0)
    assert vector.telemetry_seen_count == 1


@pytest.mark.parametrize("channel", ["telemetry", "physio", "kinematics"])
def test_fusion_rejects_nonfinite_source_time(channel: str) -> None:
    # Reject before the channel can be changed, regardless of payload.
    with pytest.raises(ValueError, match="finite"):
        getattr(FeatureFusion(), f"update_{channel}")(None, timestamp=float("nan"))
