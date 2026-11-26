"""Qualification harness: synthetic truth, production replay, agreement arithmetic."""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pytest

from cortex.services.eval.physio_qualification import (
    METRIC_HEART_RATE,
    METRIC_RESPIRATION,
    METRIC_RMSSD,
    METRIC_SDNN,
    TOLERANCES,
    StrideRecord,
    WindowedValue,
    collect_pairs,
    false_publication_rate,
    merge_reports,
    replay_trace,
    score_pairs,
)
from cortex.services.eval.physio_synthetic import (
    SyntheticRecording,
    SyntheticSubject,
    signal_free_recording,
    synthesize,
)

CLEAN = SyntheticSubject(
    duration_seconds=60.0, modulation_depth=0.02, sensor_noise_fraction=0.0008, seed=11
)


@pytest.fixture(scope="module")
def clean_recording() -> SyntheticRecording:
    return synthesize(CLEAN)


@pytest.fixture(scope="module")
def clean_records(clean_recording: SyntheticRecording) -> list[StrideRecord]:
    return replay_trace(
        clean_recording.sample_times_s,
        clean_recording.rgb,
        clean_recording.head_vertical_face_units,
    )


def _heart_rate_report(
    recording: SyntheticRecording, records: list[StrideRecord]
) -> tuple[float | None, float]:
    pairs, attempted = collect_pairs(records, heart_rate_truth=recording.true_heart_rate_bpm)[
        METRIC_HEART_RATE
    ]
    report = score_pairs(
        METRIC_HEART_RATE, pairs, attempted=attempted, tolerance=TOLERANCES[METRIC_HEART_RATE]
    )
    return report.mae, report.coverage


# ---------------------------------------------------------------------------
# Synthetic generator
# ---------------------------------------------------------------------------


def test_generator_is_deterministic_in_its_seed() -> None:
    first, again = synthesize(SyntheticSubject(duration_seconds=20.0, seed=3)), synthesize(
        SyntheticSubject(duration_seconds=20.0, seed=3)
    )
    other = synthesize(SyntheticSubject(duration_seconds=20.0, seed=4))
    for name in ("sample_times_s", "rgb", "head_vertical_face_units", "beat_times_s", "ibi_ms"):
        assert np.array_equal(getattr(first, name), getattr(again, name)), name
    assert not np.array_equal(first.rgb, other.rgb)
    assert not np.array_equal(first.beat_times_s, other.beat_times_s)

    free = signal_free_recording(duration_seconds=10.0, kind="pink", seed=2)
    free_again = signal_free_recording(duration_seconds=10.0, kind="pink", seed=2)
    assert np.array_equal(free.rgb, free_again.rgb)
    assert free.beat_times_s.size == 0


def test_truth_helpers_agree_with_the_beat_list() -> None:
    recording = synthesize(SyntheticSubject(duration_seconds=120.0, seed=8))
    assert np.allclose(recording.ibi_ms, np.diff(recording.beat_times_s) * 1_000.0)
    rng = np.random.default_rng(1)
    for _ in range(20):
        start = float(rng.uniform(0.0, 80.0))
        end = start + float(rng.uniform(5.0, 40.0))
        inside = recording.beat_times_s[
            (recording.beat_times_s >= start) & (recording.beat_times_s <= end)
        ]
        intervals_ms = np.diff(inside) * 1_000.0
        assert recording.true_heart_rate_bpm(start, end) == pytest.approx(
            60_000.0 / float(np.mean(intervals_ms))
        )
        assert recording.true_sdnn_ms(start, end) == pytest.approx(
            float(np.std(intervals_ms, ddof=1))
        )
        assert recording.true_rmssd_ms(start, end) == pytest.approx(
            float(np.sqrt(np.mean(np.diff(intervals_ms) ** 2)))
        )
    # Over the whole recording the rate is the configured one.
    assert recording.true_heart_rate_bpm(0.0, 120.0) == pytest.approx(72.0, abs=1.0)
    # Too few beats in the interval: truth abstains.
    assert recording.true_heart_rate_bpm(10.0, 10.5) is None
    assert recording.true_sdnn_ms(10.0, 11.2) is None


def test_generator_renders_drift_and_dropped_frames() -> None:
    drifting = synthesize(
        SyntheticSubject(duration_seconds=240.0, heart_rate_drift_bpm_per_min=5.0, seed=2)
    )
    early = drifting.true_heart_rate_bpm(0.0, 30.0)
    late = drifting.true_heart_rate_bpm(210.0, 240.0)
    assert early is not None and late is not None
    # Window centres are 3.5 min apart.
    assert late - early == pytest.approx(5.0 * 3.5, abs=2.5)

    dropping = synthesize(
        SyntheticSubject(duration_seconds=60.0, dropped_frame_probability=0.2, seed=2)
    )
    assert 0.7 * 1_800 < dropping.sample_times_s.size < 0.9 * 1_800
    assert bool(np.all(np.diff(dropping.sample_times_s) > 0.0))


# ---------------------------------------------------------------------------
# Production replay
# ---------------------------------------------------------------------------


def test_clean_recording_replays_accurately_with_high_coverage(
    clean_recording: SyntheticRecording, clean_records: list[StrideRecord]
) -> None:
    mae, coverage = _heart_rate_report(clean_recording, clean_records)
    assert mae is not None and mae < 2.0
    assert coverage > 0.8
    # One record per production stride (1 s) over the recording.
    assert len(clean_records) == pytest.approx(CLEAN.duration_seconds, abs=2)


def test_invalid_samples_lower_coverage_but_not_accuracy(
    clean_recording: SyntheticRecording, clean_records: list[StrideRecord]
) -> None:
    times = clean_recording.sample_times_s
    mask = ~(((times >= 20.0) & (times < 26.0)) | ((times >= 40.0) & (times < 46.0)))
    masked = replay_trace(
        times, clean_recording.rgb, clean_recording.head_vertical_face_units, valid_mask=mask
    )
    full_mae, full_coverage = _heart_rate_report(clean_recording, clean_records)
    masked_mae, masked_coverage = _heart_rate_report(clean_recording, masked)
    assert masked_coverage < full_coverage - 0.1
    assert masked_mae is not None and masked_mae < 2.0
    # Nothing is published from a window that spans a gap.
    for record in masked:
        if record.heart_rate is not None:
            start, end = record.heart_rate.start_s, record.heart_rate.end_s
            assert not (start < 26.0 and end > 20.0 + 0.25)
            assert not (start < 46.0 and end > 40.0 + 0.25)


def test_non_finite_samples_are_delivered_as_rejected_observations(
    clean_recording: SyntheticRecording,
) -> None:
    times = clean_recording.sample_times_s
    gap = (times >= 30.0) & (times < 33.0)
    rgb = clean_recording.rgb.copy()
    rgb[gap] = np.nan
    with_nan = replay_trace(times, rgb, clean_recording.head_vertical_face_units)
    with_mask = replay_trace(
        times, clean_recording.rgb, clean_recording.head_vertical_face_units, valid_mask=~gap
    )
    assert [r.heart_rate for r in with_nan] == [r.heart_rate for r in with_mask]


def test_replay_rejects_malformed_input() -> None:
    times = np.arange(10, dtype=np.float64) / 30.0
    with pytest.raises(ValueError, match="aligned"):
        replay_trace(times, np.ones((10, 2)), None)
    with pytest.raises(ValueError, match="aligned"):
        replay_trace(times, np.ones((9, 3)), None)
    with pytest.raises(ValueError, match="strictly increasing"):
        replay_trace(times[::-1].copy(), np.ones((10, 3)), None)
    with pytest.raises(ValueError, match="strictly increasing"):
        replay_trace(np.zeros(10), np.ones((10, 3)), None)
    with pytest.raises(ValueError, match="strictly increasing"):
        replay_trace(times[:1], np.ones((1, 3)), None)


def test_generators_reject_invalid_configuration() -> None:
    with pytest.raises(ValueError):
        synthesize(SyntheticSubject(fps=0.0))
    with pytest.raises(ValueError):
        synthesize(SyntheticSubject(duration_seconds=0.0))
    with pytest.raises(ValueError, match="unknown signal-free kind"):
        signal_free_recording(duration_seconds=5.0, kind="brown")


# ---------------------------------------------------------------------------
# Agreement arithmetic
# ---------------------------------------------------------------------------


def test_score_pairs_arithmetic() -> None:
    pairs = [(72.0, 70.0), (75.0, 74.0), (68.0, 70.0), (80.0, 76.0)]  # errors 2, 1, -2, 4
    report = score_pairs(METRIC_HEART_RATE, pairs, attempted=8, tolerance=2.0)
    assert report.published == 4 and report.attempted == 8
    assert report.coverage == pytest.approx(0.5)
    assert report.bias == pytest.approx(1.25)
    assert report.mae == pytest.approx(2.25)
    assert report.rmse == pytest.approx(2.5)
    # Sample SD of the errors is 2.5, so the limits are bias +/- 4.9.
    assert report.loa_lower == pytest.approx(1.25 - 1.96 * 2.5)
    assert report.loa_upper == pytest.approx(1.25 + 1.96 * 2.5)
    assert report.within_tolerance == pytest.approx(0.75)
    assert report.p95_abs_error == pytest.approx(3.7)
    assert report.pearson_r is not None and -1.0 <= report.pearson_r <= 1.0
    assert report.to_dict()["metric"] == METRIC_HEART_RATE


def test_score_pairs_counts_abstentions_and_handles_degenerate_input() -> None:
    empty = score_pairs(METRIC_SDNN, [], attempted=12, tolerance=15.0)
    assert empty.coverage == 0.0 and empty.published == 0
    assert empty.mae is None and empty.bias is None and empty.loa_lower is None
    assert score_pairs(METRIC_SDNN, [], attempted=0, tolerance=15.0).coverage == 0.0

    single = score_pairs(METRIC_HEART_RATE, [(71.0, 70.0)], attempted=1, tolerance=5.0)
    assert single.coverage == 1.0
    assert single.loa_lower == single.loa_upper == pytest.approx(1.0)
    assert single.pearson_r is None

    constant = score_pairs(METRIC_HEART_RATE, [(70.0, 70.0)] * 5, attempted=5, tolerance=5.0)
    assert constant.pearson_r is None and constant.mae == 0.0


def _record(at_s: float, heart_rate: WindowedValue | None = None) -> StrideRecord:
    return StrideRecord(at_s=at_s, heart_rate=heart_rate, rmssd=None, sdnn=None, respiration=None)


def test_collect_pairs_scores_each_estimate_over_its_own_window() -> None:
    calls: list[tuple[float, float]] = []

    def truth(start: float, end: float) -> float | None:
        calls.append((start, end))
        return 70.0

    published = _record(20.0, WindowedValue(value=72.0, start_s=3.0, end_s=13.0, quality=0.9))
    abstained = _record(30.0)
    collected = collect_pairs([published, abstained], heart_rate_truth=truth)
    pairs, attempted = collected[METRIC_HEART_RATE]
    assert pairs == [(72.0, 70.0)]
    assert attempted == 2
    # The published value is scored over (3, 13), not the stride's trailing window.
    assert calls == [(3.0, 13.0), (20.0, 30.0)]
    assert set(collected) == {METRIC_HEART_RATE}


def test_collect_pairs_counts_attempts_only_where_truth_exists() -> None:
    records = [_record(float(t)) for t in range(0, 60, 5)]

    def truth_after_30(start: float, end: float) -> float | None:
        return 70.0 if end >= 30.0 else None

    pairs, attempted = collect_pairs(records, heart_rate_truth=truth_after_30)[METRIC_HEART_RATE]
    assert pairs == [] and attempted == 6
    _, warm = collect_pairs(records, heart_rate_truth=truth_after_30, warmup_seconds=45.0)[
        METRIC_HEART_RATE
    ]
    assert warm == 3


def test_collect_pairs_uses_each_metrics_nominal_window_when_abstaining() -> None:
    windows: dict[str, tuple[float, float]] = {}

    def recorder(name: str) -> Callable[[float, float], float | None]:
        def _truth(start: float, end: float) -> float | None:
            windows[name] = (start, end)
            return None

        return _truth

    collect_pairs(
        [_record(400.0)],
        heart_rate_truth=recorder(METRIC_HEART_RATE),
        rmssd_truth=recorder(METRIC_RMSSD),
        sdnn_truth=recorder(METRIC_SDNN),
        respiration_truth=recorder(METRIC_RESPIRATION),
    )
    assert windows == {
        METRIC_HEART_RATE: (390.0, 400.0),
        METRIC_RMSSD: (280.0, 400.0),
        METRIC_SDNN: (100.0, 400.0),
        METRIC_RESPIRATION: (355.0, 400.0),
    }
    collect_pairs([_record(20.0)], sdnn_truth=recorder(METRIC_SDNN))
    assert windows[METRIC_SDNN] == (0.0, 20.0)


def test_merge_reports_pools_pairs_and_attempts() -> None:
    merged = merge_reports(
        [
            {METRIC_HEART_RATE: ([(72.0, 70.0)], 2)},
            {METRIC_HEART_RATE: ([(69.0, 70.0), (71.0, 70.0)], 4)},
        ]
    )
    report = merged[METRIC_HEART_RATE]
    assert report.published == 3 and report.attempted == 6
    assert report.coverage == pytest.approx(0.5)
    assert report.tolerance == TOLERANCES[METRIC_HEART_RATE]


def test_false_publication_rate_counts_strides() -> None:
    assert false_publication_rate([]) == {}
    value = WindowedValue(value=70.0, start_s=0.0, end_s=10.0, quality=0.5)
    rates = false_publication_rate([_record(1.0, value), _record(2.0), _record(3.0), _record(4.0)])
    assert rates[METRIC_HEART_RATE] == pytest.approx(0.25)
    assert rates[METRIC_SDNN] == 0.0
