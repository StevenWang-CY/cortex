"""Versioned timestamp contracts preserve provenance during v1 -> v2."""

from __future__ import annotations

import pytest

from cortex.application.clock import FakeClock
from cortex.libs.schemas.features import FeatureVector, FrameMeta
from cortex.libs.schemas.state import StateScores
from cortex.libs.schemas.temporal import EventTime
from cortex.services.state_engine.feature_fusion import FeatureFusion
from cortex.services.state_engine.smoother import ScoreSmoother


def test_capture_legacy_timestamp_remains_epoch_seconds() -> None:
    """FrameMeta's legacy field mirrors its historical wall-clock producer."""

    clock = FakeClock(wall_unix_ms=1_700_000_000_123, mono_ns=250_000_000)
    event = EventTime.from_clock(clock)
    frame = FrameMeta(
        timestamp=event.observed_at_unix_ms / 1_000,
        observed_at_unix_ms=event.observed_at_unix_ms,
        observed_at_mono_ns=event.observed_at_mono_ns,
        boot_id=event.boot_id,
        face_detected=True,
        face_confidence=0.9,
        brightness_score=0.8,
        blur_score=0.9,
        motion_score=0.1,
    )

    assert frame.timestamp == 1_700_000_000.123
    assert frame.observed_at_mono_ns == 250_000_000


def test_state_pipeline_legacy_timestamp_remains_monotonic() -> None:
    """The deprecated state timestamp is not silently redefined as wall time."""

    clock = FakeClock(wall_unix_ms=1_700_000_000_123, mono_ns=250_000_000)
    event = EventTime.from_clock(clock)
    vector, quality = FeatureFusion(clock=clock).fuse(event_time=event)
    estimate = ScoreSmoother(clock=clock).update(
        StateScores(flow=1.0),
        quality,
        event_time=event,
    )

    assert vector.timestamp == 0.25
    assert estimate.timestamp == 0.25
    assert vector.observed_at_unix_ms == 1_700_000_000_123
    assert estimate.observed_at_unix_ms == 1_700_000_000_123


@pytest.mark.parametrize("model", [FrameMeta, FeatureVector])
def test_partial_v2_clock_tuple_is_rejected(
    model: type[FrameMeta] | type[FeatureVector],
) -> None:
    """A monotonic value without its boot domain must never cross a boundary."""

    kwargs: dict[str, object] = {
        "timestamp": 0.0,
        "observed_at_unix_ms": 1,
    }
    if model is FrameMeta:
        kwargs.update(
            face_detected=False,
            face_confidence=0.0,
            brightness_score=0.0,
            blur_score=0.0,
            motion_score=0.0,
        )

    with pytest.raises(ValueError, match="must be supplied together"):
        model(**kwargs)
