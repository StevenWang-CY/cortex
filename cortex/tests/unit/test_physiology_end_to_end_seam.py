"""Camera physiology from a colour trace to the rules, through every real stage.

The stages have their own tests; this one joins them the way the daemon does:

    colour trace -> CameraPhysiologyProcessor -> PhysioFeatures
                 -> FeatureFusion -> named features
                 -> PhysiologyBaselineTracker -> personal reference
                 -> evaluate_physiology / RuleScorer

A value that is measured but dropped, renamed or mis-stamped between two
stages passes every stage test and fails here.
"""

from __future__ import annotations

import pytest

from cortex.libs.schemas.features import FeatureName, FeatureVector, PhysioFeatures
from cortex.services.eval.physio_qualification import replay_trace
from cortex.services.eval.physio_synthetic import SyntheticSubject, synthesize
from cortex.services.physio_engine.camera_physiology import PhysiologyUpdate
from cortex.services.state_engine.feature_fusion import FeatureFusion
from cortex.services.state_engine.physiology_baseline import (
    PhysiologyBaselineTracker,
    PhysiologyMetricName,
)
from cortex.services.state_engine.physiology_evidence import evaluate_physiology

_METRICS = (
    (PhysiologyMetricName.HEART_RATE, FeatureName.HEART_RATE_BPM),
    (PhysiologyMetricName.SDNN, FeatureName.HRV_SDNN_MS),
    (PhysiologyMetricName.RESPIRATION, FeatureName.RESPIRATION_RATE_BPM),
)


@pytest.fixture(scope="module")
def replayed() -> tuple[SyntheticSubject, list[tuple[float, FeatureVector]], PhysiologyBaselineTracker]:
    subject = SyntheticSubject(
        heart_rate_bpm=68.0,
        respiration_rate_bpm=13.0,
        duration_seconds=300.0,
        seed=11,
    )
    recording = synthesize(subject)
    fusion = FeatureFusion()
    tracker = PhysiologyBaselineTracker()
    vectors: list[tuple[float, FeatureVector]] = []

    def _on_update(at_s: float, update: PhysiologyUpdate) -> None:
        features: PhysioFeatures = update.features
        # The daemon publishes physiology only once a pulse window exists.
        if update.pulse is None:
            return
        fusion.update_physio(features, timestamp=at_s)
        vector, _quality = fusion.fuse(timestamp=at_s)
        for metric, name in _METRICS:
            item = vector.features[name]
            if item.valid and item.value is not None:
                tracker.observe(metric, float(item.value), at_s)
        vectors.append((at_s, vector))

    replay_trace(
        recording.sample_times_s,
        recording.rgb,
        recording.head_vertical_face_units,
        on_update=_on_update,
    )
    return subject, vectors, tracker


def test_measured_values_arrive_as_valid_named_features(
    replayed: tuple[SyntheticSubject, list[tuple[float, FeatureVector]], PhysiologyBaselineTracker],
) -> None:
    subject, vectors, _tracker = replayed
    assert vectors, "the processor never produced a pulse window"
    _at, last = vectors[-1]

    heart = last.features[FeatureName.HEART_RATE_BPM]
    assert heart.valid and heart.value == pytest.approx(subject.heart_rate_bpm, abs=3.0)
    assert last.hr == pytest.approx(float(heart.value))
    # Stamped with the window the rate describes: the 20 s long window.
    assert 18_000 <= heart.source_window_ms <= 21_000
    assert heart.quality > 0.3

    sdnn = last.features[FeatureName.HRV_SDNN_MS]
    assert sdnn.valid and sdnn.value is not None and 5.0 < float(sdnn.value) < 150.0
    assert last.hrv_sdnn == pytest.approx(float(sdnn.value))
    assert sdnn.quality > 0.0

    # Breathing is published only while two channels agree, so it comes and
    # goes; every vector that carries it must carry the right rate, and every
    # vector that does not must abstain cleanly.
    with_breathing = [
        vector
        for _at, vector in vectors
        if vector.features[FeatureName.RESPIRATION_RATE_BPM].valid
    ]
    assert len(with_breathing) > 0.25 * len(vectors)
    for vector in with_breathing:
        breathing = vector.features[FeatureName.RESPIRATION_RATE_BPM]
        assert breathing.value == pytest.approx(subject.respiration_rate_bpm, abs=2.0)
        assert vector.respiration_rate == pytest.approx(float(breathing.value))
        assert breathing.quality > 0.0
    for _at, vector in vectors:
        breathing = vector.features[FeatureName.RESPIRATION_RATE_BPM]
        if not breathing.valid:
            assert breathing.value is None and vector.respiration_rate is None


def test_early_windows_abstain_instead_of_publishing_a_placeholder(
    replayed: tuple[SyntheticSubject, list[tuple[float, FeatureVector]], PhysiologyBaselineTracker],
) -> None:
    _subject, vectors, _tracker = replayed
    # Variability needs at least 90 s of beats: no vector in the first minute
    # may carry one, valid or as a number.
    early = [vector for at, vector in vectors if at < 60.0]
    assert early
    for vector in early:
        item = vector.features[FeatureName.HRV_SDNN_MS]
        assert not item.valid and item.value is None
        assert vector.hrv_sdnn is None


def test_the_session_builds_a_reference_and_reads_calm_as_calm(
    replayed: tuple[SyntheticSubject, list[tuple[float, FeatureVector]], PhysiologyBaselineTracker],
) -> None:
    subject, vectors, tracker = replayed
    reference = tracker.reference()
    assert reference.heart_rate.ready and reference.heart_rate.source == "session"
    assert reference.heart_rate.center == pytest.approx(subject.heart_rate_bpm, abs=3.0)
    assert reference.sdnn.ready
    assert reference.respiration.ready

    complete = [
        vector
        for _at, vector in vectors
        if all(vector.features[name].valid for _metric, name in _METRICS)
    ]
    assert complete, "no stride carried heart rate, SDNN and breathing together"
    evidence = evaluate_physiology(complete[-1].features, reference)
    # Three of the four support inputs are live (no blink data in this
    # replay), so the block is well covered ...
    assert evidence.support_likely.observed_count == 3
    assert evidence.support_likely.coverage > 0.3
    # ... and a steady simulated person, read against their own level, shows
    # next to no strain.
    assert evidence.support_likely.score < 0.25 * evidence.support_likely.coverage
    assert evidence.flow_like.score > 0.5 * evidence.flow_like.coverage
