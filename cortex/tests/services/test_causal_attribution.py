"""P0 §3.9: causal attribution unit tests."""

from __future__ import annotations

import pytest

from cortex.libs.schemas.features import FeatureVector
from cortex.libs.schemas.state import UserBaselines
from cortex.services.state_engine.causal_attribution import (
    CausalAttributor,
    attribute_top_signals,
)


def _make_features(**overrides: object) -> FeatureVector:
    base: dict[str, object] = {
        "timestamp": 0.0,
        "hr": 72.0,
        "hrv_rmssd": 50.0,
        "blink_rate": 17.0,
        "tab_switch_frequency": 2.0,
        "forward_lean_angle": 4.0,
    }
    base.update(overrides)
    return FeatureVector(**base)


def test_hrv_drop_attributed_as_primary() -> None:
    baselines = UserBaselines(
        hrv_baseline=50.0,
        metric_distributions={"hrv_rmssd": {"mu": 50.0, "sigma": 5.0}},
    )
    # Strong HRV drop (z = -4 → 4 after direction flip), everything else baseline.
    features = _make_features(hrv_rmssd=30.0)
    signals = attribute_top_signals(features, baselines, max_signals=3)
    assert signals, "expected non-empty causal signals for an HRV deficit"
    primary = signals[0]
    assert primary.name == "HRV"
    assert primary.severity == "primary"
    assert primary.current_value == pytest.approx(30.0)
    assert primary.baseline_value == pytest.approx(50.0)
    assert primary.delta_pct is not None
    assert primary.delta_pct < 0  # HRV dropped


def test_sparkline_buffers_propagate_to_output() -> None:
    attributor = CausalAttributor()
    # Feed three increasing HRV readings — last is the "current".
    for hrv in (30.0, 32.0, 34.0):
        attributor.record_feature_vector(_make_features(hrv_rmssd=hrv))
    signals = attributor.attribute_top_signals(
        _make_features(hrv_rmssd=34.0),
        UserBaselines(hrv_baseline=50.0),
    )
    hrv_signal = next((s for s in signals if s.name == "HRV"), None)
    assert hrv_signal is not None
    # The buffer captured 3 frames of recordings, plus the live current is
    # re-extracted at attribute time so the list length is 3.
    assert len(hrv_signal.samples_60s) == 3
    assert hrv_signal.samples_60s == pytest.approx([30.0, 32.0, 34.0])


def test_top_three_signals_capped_with_severity_tiers() -> None:
    baselines = UserBaselines(
        hrv_baseline=50.0,
        hr_baseline=72.0,
        blink_rate_baseline=17.0,
    )
    # Push every signal into anomalous territory; expect top-3 ordering.
    features = _make_features(
        hrv_rmssd=28.0,            # HRV deficit (primary candidate)
        hr=98.0,                   # heart rate spike
        tab_switch_frequency=10.0,  # thrashing
        forward_lean_angle=18.0,   # leaning in
        perclos_60s=0.4,           # eyes closing
    )
    signals = attribute_top_signals(features, baselines, max_signals=3)
    assert 0 < len(signals) <= 3
    severities = [s.severity for s in signals]
    assert severities[0] == "primary"
    if len(signals) > 1:
        assert severities[1] == "secondary"
    if len(signals) > 2:
        assert severities[2] == "tertiary"


def test_empty_features_returns_empty_list() -> None:
    features = FeatureVector(timestamp=0.0)
    # No baseline crossings (everything is None) — nothing to attribute.
    signals = attribute_top_signals(features, UserBaselines())
    # Audit fix: when feature extraction fails for every signal,
    # return an empty list. The UI renders an "explanation pending"
    # empty state for causal_signals.length === 0.
    assert signals == []


def test_within_baseline_returns_empty_list() -> None:
    """Audit fix: a user well within their envelope yields no
    causal signals — the previous synthetic "HRV (baseline)" placeholder
    showed a real biometric value next to a label the user couldn't act
    on. The UI's empty-state ("explanation pending") communicates the
    healthy baseline more honestly.
    """
    baselines = UserBaselines(hrv_baseline=50.0)
    # All signals at baseline — every z-score is 0, nothing exceeds
    # the floor, so we get an empty list.
    features = FeatureVector(
        timestamp=0.0,
        hr=72.0,
        hrv_rmssd=50.0,
        blink_rate=17.0,
        tab_switch_frequency=2.0,
        forward_lean_angle=4.0,
    )
    signals = attribute_top_signals(features, baselines)
    assert signals == []


def test_real_signal_never_mixes_with_synthetic_fallback() -> None:
    """When at least one real anomalous signal is present, the result
    contains only that real signal — no synthetic baseline-style
    placeholder is appended.
    """
    baselines = UserBaselines(
        hrv_baseline=50.0,
        hr_baseline=72.0,
        metric_distributions={"hrv_rmssd": {"mu": 50.0, "sigma": 5.0}},
    )
    # Only HRV deviates; HR is at baseline so it should not appear.
    features = _make_features(hrv_rmssd=28.0)
    signals = attribute_top_signals(features, baselines, max_signals=3)
    assert len(signals) == 1
    assert signals[0].name == "HRV"
    # No baseline-style synthetic label anywhere in the result.
    assert not any("baseline" in s.name.lower() for s in signals)


def test_attributor_reset_clears_buffers() -> None:
    attributor = CausalAttributor()
    for _ in range(5):
        attributor.record_feature_vector(_make_features(hrv_rmssd=30.0))
    attributor.reset()
    signals = attributor.attribute_top_signals(
        _make_features(hrv_rmssd=30.0), UserBaselines(),
    )
    # After reset the live current is the only sample for any signal.
    hrv_signal = next((s for s in signals if s.name == "HRV"), None)
    assert hrv_signal is not None
    # The buffer was cleared, then ``attribute_top_signals`` reads the
    # current value once → list is empty (live current is not appended
    # by attribution itself, only by record_feature_vector).
    assert hrv_signal.samples_60s == []
