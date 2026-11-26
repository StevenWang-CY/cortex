"""Deterministic evidence-contract tests for physiology v2."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import UUID

import numpy as np
import pytest

from cortex.libs.config.settings import RPPGSignalConfig
from cortex.libs.schemas.physiology import (
    BeatCandidate,
    BeatStatus,
    EvidenceStatus,
    InterBeatInterval,
    PhysiologyMetric,
)
from cortex.services.physio_engine.quality_scorer import QualityScorer
from cortex.services.physio_engine.rppg import RPPGAlgorithm, extract_bvp
from cortex.services.physio_engine.v2.backends import (
    BackendDefinition,
    BackendValidationError,
    RPPGBackendRegistry,
)
from cortex.services.physio_engine.v2.beats import BeatLedger
from cortex.services.physio_engine.v2.engine import PhysiologyEngineV2
from cortex.services.physio_engine.v2.hrv import (
    build_hrv_estimates,
    rmssd_ms,
    select_interval_series,
)
from cortex.services.physio_engine.v2.pulse import PulsePipelineV2
from cortex.services.physio_engine.v2.replay import (
    DatasetManifestError,
    evaluate_dataset_manifest,
    load_dataset_manifest,
)
from cortex.services.physio_engine.v2.respiration import RespirationFusionV2

_BOOT = UUID("22222222-2222-2222-2222-222222222222")


def _rgb_trace(
    times_s: np.ndarray,
    *,
    heart_hz: float = 1.2,
    respiration_hz: float | None = None,
    harmonic_amplitude: float = 0.0,
    drift_amplitude: float = 0.0,
) -> np.ndarray:
    pulse = np.sin(2 * np.pi * heart_hz * times_s)
    pulse += harmonic_amplitude * np.sin(2 * np.pi * 2.0 * heart_hz * times_s)
    if respiration_hz is not None:
        pulse *= 1.0 + 0.30 * np.sin(2 * np.pi * respiration_hz * times_s)
    drift = drift_amplitude * np.sin(2 * np.pi * 0.05 * times_s)
    return np.column_stack(
        [100.0 + 0.4 * pulse + drift, 90.0 + 1.5 * pulse + drift, 80.0 + 0.2 * pulse + drift]
    ).astype(np.float64)


def _pipeline() -> PulsePipelineV2:
    backend = RPPGBackendRegistry.with_packaged_backends().resolve("pos")
    return PulsePipelineV2(backend)


def _candidate(
    mono_ns: int,
    *,
    window: str,
    quality: float = 0.9,
    prominence: float = 1.0,
    boundary: bool = False,
) -> BeatCandidate:
    return BeatCandidate(
        candidate_id=f"{window}-{mono_ns}",
        absolute_mono_ns=mono_ns,
        prominence=prominence,
        quality=quality,
        source_window_id=window,
        near_window_boundary=boundary,
    )


def test_backend_identity_is_stable_before_and_after_execution() -> None:
    registry = RPPGBackendRegistry.with_packaged_backends()
    first = registry.resolve("pos")
    t = np.arange(300, dtype=np.float64) / 30.0
    first.extract(_rgb_trace(t), fs=30.0)
    second = registry.resolve("pos")
    assert first.identity == second.identity
    assert len(first.identity.implementation_sha256) == 64


def test_signal_configuration_changes_have_distinct_provenance() -> None:
    backend = RPPGBackendRegistry.with_packaged_backends().resolve("pos")
    default = PulsePipelineV2(backend)
    changed = PulsePipelineV2(backend, low_hz=0.8)
    fs = 30.0
    t = np.arange(300, dtype=np.float64) / fs
    times = np.rint(t * 1e9).astype(np.int64)
    first = default.process_window(
        _rgb_trace(t),
        times,
        sample_rate_hz=fs,
        boot_id=_BOOT,
        observation_quality=1.0,
    )
    second = changed.process_window(
        _rgb_trace(t),
        times,
        sample_rate_hz=fs,
        boot_id=_BOOT,
        observation_quality=1.0,
    )
    assert first.summary.algorithm.implementation_sha256 == (
        second.summary.algorithm.implementation_sha256
    )
    assert first.summary.algorithm.configuration_sha256 != (
        second.summary.algorithm.configuration_sha256
    )


def test_backend_never_silently_substitutes_unknown_or_checksum_mismatch() -> None:
    registry = RPPGBackendRegistry.with_packaged_backends()
    with pytest.raises(BackendValidationError, match="no fallback"):
        registry.resolve("tscan")
    with pytest.raises(BackendValidationError, match="checksum mismatch"):
        registry.resolve("pos", expected_implementation_sha256="0" * 64)
    with pytest.raises(ValueError, match="Unsupported rPPG backend"):
        extract_bvp(np.ones((30, 3)), algorithm="tscan")


def test_backend_asset_missing_and_corrupt_fail_closed(tmp_path: Path) -> None:
    def extractor(rgb: np.ndarray, fs: float) -> np.ndarray:
        del fs
        return rgb[:, 1]

    asset = tmp_path / "model.bin"
    expected = hashlib.sha256(b"model-v1").hexdigest()
    registry = RPPGBackendRegistry()
    registry.register(
        BackendDefinition(
            algorithm=RPPGAlgorithm.POS,
            version="test/1",
            extractor=extractor,
            asset_path=asset,
            asset_sha256=expected,
        )
    )
    with pytest.raises(BackendValidationError, match="missing"):
        registry.resolve("pos")
    asset.write_bytes(b"wrong")
    with pytest.raises(BackendValidationError, match="asset checksum mismatch"):
        registry.resolve("pos")
    asset.write_bytes(b"model-v1")
    assert registry.resolve("pos").identity.asset_sha256 == expected


def test_dynamic_backend_policy_requires_validation_artifact() -> None:
    config = RPPGSignalConfig(dynamic_backend_selection=True)
    with pytest.raises(BackendValidationError, match="held-out validation"):
        PhysiologyEngineV2(config)


def test_quality_scorer_stays_on_fixed_backend_without_validation() -> None:
    scorer = QualityScorer(initial_algorithm=RPPGAlgorithm.CHROM)
    noise_rgb = np.random.default_rng(7).normal(128.0, 10.0, size=(300, 3))
    noise = np.random.default_rng(8).normal(size=300)
    for _ in range(20):
        scorer.update(noise_rgb, noise, fs=30.0)
    assert scorer.current_algorithm is RPPGAlgorithm.CHROM


def test_pulse_result_has_algorithm_quality_uncertainty_and_experimental_status() -> None:
    fs = 30.0
    t = np.arange(300, dtype=np.float64) / fs
    times = 5_000_000_000 + np.rint(t * 1e9).astype(np.int64)
    result = _pipeline().process_window(
        _rgb_trace(t, drift_amplitude=2.0),
        times,
        sample_rate_hz=fs,
        boot_id=_BOOT,
        observation_quality=0.95,
    )
    assert result.summary.hr.status == EvidenceStatus.EXPERIMENTAL.value
    assert result.summary.hr.value == pytest.approx(72.0, abs=3.0)
    assert result.summary.hr.uncertainty is not None
    assert result.summary.algorithm.name == "pulse-v2:pos"
    assert result.summary.algorithm.configuration_sha256 is not None
    assert result.summary.algorithm.parameters["bvp_backend"] == "pos"
    assert result.summary.quality > 0.3


def test_motion_or_acquisition_failure_cannot_be_erased_by_clean_spectrum() -> None:
    fs = 30.0
    t = np.arange(300, dtype=np.float64) / fs
    times = 2_000_000_000 + np.rint(t * 1e9).astype(np.int64)
    result = _pipeline().process_window(
        _rgb_trace(t),
        times,
        sample_rate_hz=fs,
        boot_id=_BOOT,
        observation_quality=0.0,
        head_jitter_deg=30.0,
    )
    assert result.summary.hr.status == EvidenceStatus.REJECTED.value
    assert result.summary.hr.value is None


def test_harmonic_dominance_and_step_changes_do_not_lock_wrong_rate() -> None:
    fs = 30.0
    t = np.arange(300, dtype=np.float64) / fs
    pipeline = _pipeline()
    # A pulse with a strong (but not dominant) second harmonic publishes its
    # fundamental.
    rich = _pipeline().process_window(
        _rgb_trace(t, harmonic_amplitude=0.6),
        np.rint(t * 1e9).astype(np.int64),
        sample_rate_hz=fs,
        boot_id=_BOOT,
        observation_quality=0.95,
    )
    assert rich.summary.hr.value == pytest.approx(72.0, abs=3.0)

    # A window whose second harmonic *dominates* is ambiguous by itself: the
    # same spectrum is a real pulse at the harmonic plus a weak half-rate
    # line (measured on 4TU P2LC3). The gate may withhold it; it must never
    # publish the harmonic, and the selector still prefers the fundamental.
    harmonic = pipeline.process_window(
        _rgb_trace(t, harmonic_amplitude=1.5),
        np.rint(t * 1e9).astype(np.int64),
        sample_rate_hz=fs,
        boot_id=_BOOT,
        observation_quality=0.95,
    )
    assert harmonic.summary.hr.value is None or harmonic.summary.hr.value == pytest.approx(
        72.0, abs=3.0
    )

    stepped = pipeline.process_window(
        _rgb_trace(t, heart_hz=1.6),
        10_000_000_000 + np.rint(t * 1e9).astype(np.int64),
        sample_rate_hz=fs,
        boot_id=_BOOT,
        observation_quality=0.95,
    )
    assert stepped.summary.hr.value == pytest.approx(96.0, abs=3.0)


def test_overlapping_windows_are_idempotent_chronological_and_unique() -> None:
    fs = 30.0
    full_t = np.arange(int(22 * fs), dtype=np.float64) / fs
    rgb = _rgb_trace(full_t)
    mono = 10_000_000_000 + np.rint(full_t * 1e9).astype(np.int64)
    pipeline = _pipeline()
    last = None
    for second in range(0, 12):
        start = second * int(fs)
        end = start + int(10 * fs)
        last = pipeline.process_window(
            rgb[start:end],
            mono[start:end],
            sample_rate_hz=fs,
            boot_id=_BOOT,
            observation_quality=0.95,
        )
    assert last is not None
    before = (len(last.beat_events), len(last.intervals))
    repeated = pipeline.process_window(
        rgb[start:end],
        mono[start:end],
        sample_rate_hz=fs,
        boot_id=_BOOT,
        observation_quality=0.95,
    )
    assert (len(repeated.beat_events), len(repeated.intervals)) == before

    accepted = [event for event in repeated.beat_events if event.status == "accepted"]
    accepted_times = [event.absolute_mono_ns for event in accepted]
    assert accepted_times == sorted(set(accepted_times))
    ibi_ids = [interval.ibi_id for interval in repeated.intervals]
    assert len(ibi_ids) == len(set(ibi_ids))
    for interval in repeated.intervals:
        assert interval.start_mono_ns < interval.end_mono_ns
        assert interval.duration_ms == pytest.approx(
            (interval.end_mono_ns - interval.start_mono_ns) / 1_000_000.0
        )


def test_boundary_peak_confirmation_and_low_quality_rejection() -> None:
    ledger = BeatLedger()
    first, _ = ledger.ingest(
        [_candidate(9_700_000_000, window="w1", boundary=True)],
        window_id="w1",
        window_start_mono_ns=0,
        window_end_mono_ns=10_000_000_000,
        boundary_margin_ns=750_000_000,
    )
    assert first[0].status == BeatStatus.PROVISIONAL.value
    second, _ = ledger.ingest(
        [
            _candidate(9_710_000_000, window="w2", boundary=False),
            _candidate(11_000_000_000, window="w2", quality=0.05),
        ],
        window_id="w2",
        window_start_mono_ns=2_000_000_000,
        window_end_mono_ns=12_000_000_000,
        boundary_margin_ns=750_000_000,
    )
    confirmed = [item for item in second if len(item.source_window_ids) == 2]
    assert len(confirmed) == 1
    assert confirmed[0].status == BeatStatus.ACCEPTED.value
    rejected = [item for item in second if item.status == BeatStatus.REJECTED.value]
    assert any(item.rejection_reason == "low_quality" for item in rejected)


def test_refractory_conflicts_and_implausible_ibis_are_explicit() -> None:
    ledger = BeatLedger(min_hr_bpm=40.0, max_hr_bpm=200.0)
    candidates = [
        _candidate(1_000_000_000, window="w", quality=0.9),
        _candidate(1_200_000_000, window="w", quality=0.3),
        _candidate(2_000_000_000, window="w", quality=0.9),
        _candidate(6_000_000_000, window="w", quality=0.9),
    ]
    events, intervals = ledger.ingest(
        candidates,
        window_id="w",
        window_start_mono_ns=0,
        window_end_mono_ns=7_000_000_000,
        boundary_margin_ns=0,
    )
    assert any(item.rejection_reason == "refractory_conflict" for item in events)
    assert any(item.rejection_reason == "ibi_too_long" for item in intervals)


def test_hrv_metrics_each_follow_their_own_contract() -> None:
    ledger = BeatLedger(history_seconds=700.0)
    candidates = [
        _candidate(i * 1_000_000_000, window="long")
        for i in range(1, 362)
    ]
    _, intervals = ledger.ingest(
        candidates,
        window_id="long",
        window_start_mono_ns=0,
        window_end_mono_ns=362_000_000_000,
        boundary_margin_ns=0,
    )
    identity = RPPGBackendRegistry.with_packaged_backends().resolve("pos").identity
    disabled = build_hrv_estimates(
        intervals, algorithm=identity, boot_id=_BOOT, enabled=False
    )
    assert all(estimate.value is None for estimate in disabled.values())
    assert "turned off" in (disabled[PhysiologyMetric.RMSSD].unavailable_reason or "")
    assert disabled[PhysiologyMetric.RMSSD].algorithm.name.startswith("hrv-v2:")
    assert disabled[PhysiologyMetric.RMSSD].algorithm != identity
    assert disabled[PhysiologyMetric.RMSSD].algorithm.configuration_sha256 is not None

    enabled = build_hrv_estimates(
        intervals, algorithm=identity, boot_id=_BOOT, enabled=True
    )
    # A perfectly regular beat train has no variability to report...
    assert enabled[PhysiologyMetric.RMSSD].status == "experimental"
    assert enabled[PhysiologyMetric.RMSSD].value == pytest.approx(0.0)
    assert enabled[PhysiologyMetric.SDNN].value == pytest.approx(0.0)
    assert enabled[PhysiologyMetric.PNN50].value == pytest.approx(0.0)
    # ...and no spectrum or entropy to estimate, which is an abstention with
    # a reason rather than a fabricated zero.
    for metric in (PhysiologyMetric.LF_HF_RATIO, PhysiologyMetric.SAMPLE_ENTROPY):
        assert enabled[metric].status == "unavailable"
        assert enabled[metric].unavailable_reason


def test_hrv_needs_enough_recent_clean_beats() -> None:
    ledger = BeatLedger()
    # Thirty seconds of beats: below the sixty-second contract.
    candidates = [_candidate(i * 1_000_000_000, window="short") for i in range(1, 31)]
    _, intervals = ledger.ingest(
        candidates,
        window_id="short",
        window_start_mono_ns=0,
        window_end_mono_ns=31_000_000_000,
        boundary_margin_ns=0,
    )
    identity = RPPGBackendRegistry.with_packaged_backends().resolve("pos").identity
    estimates = build_hrv_estimates(
        intervals, algorithm=identity, boot_id=_BOOT, enabled=True
    )
    rmssd = estimates[PhysiologyMetric.RMSSD]
    assert rmssd.value is None
    assert "clean beat" in (rmssd.unavailable_reason or "")


def test_hrv_successive_differences_do_not_span_a_rejected_beat() -> None:
    """A dropped beat must not be differenced across as if nothing happened."""

    ledger = BeatLedger()
    # 0.80 s and 0.90 s intervals alternate, then one beat is missing
    # (a 1.7 s gap), then the pattern resumes.
    stamps = [1_000_000_000]
    for index in range(1, 140):
        step = 800_000_000 if index % 2 else 900_000_000
        if index == 70:
            step = 1_700_000_000
        stamps.append(stamps[-1] + step)
    _, intervals = ledger.ingest(
        [_candidate(value, window="gap") for value in stamps],
        window_id="gap",
        window_start_mono_ns=0,
        window_end_mono_ns=stamps[-1] + 1_000_000_000,
        boundary_margin_ns=0,
    )
    series = select_interval_series(intervals, window_seconds=120.0)
    assert series.artifact_fraction > 0.0
    differences = series.successive_differences()
    # Every surviving difference is the true 100 ms alternation; the pair
    # straddling the gap is excluded instead of adding a spurious one.
    assert np.allclose(np.abs(differences), 100.0)
    assert rmssd_ms(series) == pytest.approx(100.0)


def test_timing_noise_is_removed_from_variability_in_variance() -> None:
    """Known beat-timing jitter is subtracted; a hopeless ratio abstains."""

    rng = np.random.default_rng(5)
    true_sd = 25.0
    durations = 850.0 + rng.normal(0.0, true_sd, size=160)
    stamps = np.concatenate(([1_000.0], 1_000.0 + np.cumsum(durations)))
    jitter_sigma = 12.0
    noisy = stamps + rng.normal(0.0, jitter_sigma, size=len(stamps))

    def _intervals(sigma: float | None) -> tuple[InterBeatInterval, ...]:
        ledger = BeatLedger()
        candidates = [
            BeatCandidate(
                candidate_id=f"j-{index}",
                absolute_mono_ns=int(value * 1_000_000),
                prominence=1.0,
                quality=0.9,
                source_window_id="jitter",
                timing_sigma_ms=sigma,
            )
            for index, value in enumerate(noisy)
        ]
        _, result = ledger.ingest(
            candidates,
            window_id="jitter",
            window_start_mono_ns=0,
            window_end_mono_ns=int(noisy[-1] * 1_000_000) + 1_000_000_000,
            boundary_margin_ns=0,
        )
        return result

    truth = float(np.sqrt(np.mean(np.diff(durations) ** 2)))
    raw = rmssd_ms(select_interval_series(_intervals(None), window_seconds=300.0))
    corrected_series = select_interval_series(
        _intervals(jitter_sigma), window_seconds=300.0
    )
    corrected = rmssd_ms(corrected_series)
    assert raw is not None and corrected is not None
    assert raw > truth + 8.0  # jitter inflates the uncorrected statistic
    assert abs(corrected - truth) < abs(raw - truth)
    assert 0.0 < corrected_series.timing_noise_share() < 1.0


def test_respiration_requires_agreeing_channels_and_respects_the_off_switch() -> None:
    fs = 30.0
    t = np.arange(int(45 * fs), dtype=np.float64) / fs
    times = 1_000_000_000 + np.rint(t * 1e9).astype(np.int64)
    backend = RPPGBackendRegistry.with_packaged_backends().resolve("pos")
    fusion = RespirationFusionV2(
        backend,
        minimum_channel_quality=0.20,
        experimental_publication_enabled=False,
    )
    result = fusion.process_window(
        _rgb_trace(t, respiration_hz=0.25),
        times,
        sample_rate_hz=fs,
        boot_id=_BOOT,
        head_vertical_face_units=0.5 + 0.02 * np.sin(2 * np.pi * 0.25 * t),
    )
    assert result.channels["color_envelope"].status == "experimental"
    assert result.channels["head_motion"].status == "experimental"
    # No beats were supplied, so the third channel abstains with a reason.
    assert result.channels["beat_interval"].status == "unavailable"
    assert result.fused.status == "unavailable"
    assert result.fused.value is None
    assert "turned off" in (result.fused.unavailable_reason or "")


def test_respiration_window_the_acquisition_gate_accepts_is_long_enough() -> None:
    """A ready 45 s window spans slightly under 45 s and must still be usable.

    The estimator used to demand the full nominal duration, so every window
    the acquisition gate accepted was rejected and no breathing rate could
    be published under any configuration.
    """

    config = RPPGSignalConfig()
    engine = PhysiologyEngineV2(config)
    fs = 30.0
    samples = int(config.respiration_window_seconds * fs)
    t = np.arange(samples, dtype=np.float64) / fs  # spans (samples - 1) / fs
    times = 1_000_000_000 + np.rint(t * 1e9).astype(np.int64)
    result = engine.respiration.process_window(
        _rgb_trace(t, respiration_hz=0.25),
        times,
        sample_rate_hz=fs,
        boot_id=_BOOT,
        head_vertical_face_units=0.5 + 0.02 * np.sin(2 * np.pi * 0.25 * t),
    )
    reasons = [
        estimate.unavailable_reason or ""
        for estimate in (result.fused, *result.channels.values())
    ]
    assert not any("requires at least" in reason for reason in reasons)


def test_two_agreeing_breathing_channels_outvote_a_third() -> None:
    fs = 30.0
    t = np.arange(int(45 * fs), dtype=np.float64) / fs
    times = 1_000_000_000 + np.rint(t * 1e9).astype(np.int64)
    backend = RPPGBackendRegistry.with_packaged_backends().resolve("pos")
    fusion = RespirationFusionV2(
        backend,
        minimum_channel_quality=0.20,
        experimental_publication_enabled=True,
    )
    # Beat intervals carrying respiratory sinus arrhythmia at 0.25 Hz.
    beat_times = np.arange(1.2, 45.0, 0.85)
    beat_times = beat_times + 0.03 * np.sin(2 * np.pi * 0.25 * beat_times)
    beats_ns = 1_000_000_000 + np.rint(beat_times * 1e9).astype(np.int64)
    ledger = BeatLedger()
    _, intervals = ledger.ingest(
        [_candidate(int(value), window="resp") for value in beats_ns],
        window_id="resp",
        window_start_mono_ns=int(times[0]),
        window_end_mono_ns=int(times[-1]) + 1_000_000_000,
        boundary_margin_ns=0,
    )
    result = fusion.process_window(
        _rgb_trace(t, respiration_hz=0.25),
        times,
        sample_rate_hz=fs,
        boot_id=_BOOT,
        # The head moves at an unrelated rate (fidgeting, not breathing).
        head_vertical_face_units=np.sin(2 * np.pi * 0.12 * t),
        beat_intervals=intervals,
    )
    assert result.channels["beat_interval"].value == pytest.approx(15.0, abs=1.5)
    assert result.fused.status == "experimental"
    assert result.fused.value == pytest.approx(15.0, abs=1.5)


def test_respiration_opt_in_fuses_agreement_and_abstains_on_disagreement() -> None:
    fs = 30.0
    t = np.arange(int(45 * fs), dtype=np.float64) / fs
    times = 1_000_000_000 + np.rint(t * 1e9).astype(np.int64)
    backend = RPPGBackendRegistry.with_packaged_backends().resolve("pos")
    fusion = RespirationFusionV2(
        backend,
        minimum_channel_quality=0.20,
        experimental_publication_enabled=True,
    )
    agreeing = fusion.process_window(
        _rgb_trace(t, respiration_hz=0.25),
        times,
        sample_rate_hz=fs,
        boot_id=_BOOT,
        head_vertical_face_units=np.sin(2 * np.pi * 0.25 * t),
    )
    assert agreeing.fused.status == "experimental"
    assert agreeing.fused.value == pytest.approx(15.0, abs=1.5)
    disagreeing = fusion.process_window(
        _rgb_trace(t, respiration_hz=0.25),
        times,
        sample_rate_hz=fs,
        boot_id=_BOOT,
        head_vertical_face_units=np.sin(2 * np.pi * 0.12 * t),
    )
    assert disagreeing.fused.status == "unavailable"
    assert "disagree" in (disagreeing.fused.unavailable_reason or "")


def _write_replay_manifest(
    root: Path,
    *,
    split_by_subject: dict[str, str],
) -> Path:
    sequences: list[dict[str, object]] = []
    fs = 30.0
    t = np.arange(int(12 * fs), dtype=np.float64) / fs
    for index, (subject, split) in enumerate(split_by_subject.items()):
        trace_path = root / f"trace-{index}.npz"
        np.savez(
            trace_path,
            rgb_trace=_rgb_trace(t, heart_hz=1.2 + index * 0.1),
            hr_gt=np.asarray([(1.2 + index * 0.1) * 60.0]),
        )
        sequences.append(
            {
                "subject_id": subject,
                "sequence_id": f"sequence-{index}",
                "split": split,
                "path": trace_path.name,
                "sha256": hashlib.sha256(trace_path.read_bytes()).hexdigest(),
                "sample_rate_hz": fs,
                "condition": "stationary",
            }
        )
    manifest_path = root / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "1.1",
                "dataset_name": "synthetic-contract-fixture",
                "dataset_version": "1",
                "license_name": "test-only",
                "license_url": "https://example.invalid/test-license",
                "source_url": "https://example.invalid/test-fixture",
                "citation": "Synthetic unit-test fixture; not participant data.",
                "data_use_notes": "Unit tests only.",
                "reference_sensor": "synthetic sinusoid",
                "clock_alignment_method": "shared synthetic sample clock",
                "participant_data_committed": False,
                "sequences": sequences,
            }
        ),
        encoding="utf-8",
    )
    return manifest_path


def test_checksum_verified_subject_disjoint_dataset_replay(tmp_path: Path) -> None:
    manifest_path = _write_replay_manifest(
        tmp_path,
        split_by_subject={"subject-dev": "development", "subject-eval": "evaluation"},
    )
    manifest = load_dataset_manifest(manifest_path)
    assert len(manifest.sequences) == 2
    report = evaluate_dataset_manifest(manifest_path)
    assert report.subject_count == 1
    assert report.attempted_windows == 3
    assert report.coverage == 1.0
    assert report.mae_bpm is not None and report.mae_bpm < 3.0
    assert report.p95_absolute_error_bpm is not None
    assert report.conditions[0].condition == "stationary"
    assert len(report.backend_sha256) == 64


def test_dataset_replay_rejects_checksum_drift_and_subject_leakage(
    tmp_path: Path,
) -> None:
    manifest_path = _write_replay_manifest(
        tmp_path,
        split_by_subject={"same-subject": "development"},
    )
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    duplicate = dict(raw["sequences"][0])
    duplicate["sequence_id"] = "evaluation-copy"
    duplicate["split"] = "evaluation"
    raw["sequences"].append(duplicate)
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(DatasetManifestError, match="both development and evaluation"):
        load_dataset_manifest(manifest_path)

    raw["sequences"] = raw["sequences"][:1]
    raw["sequences"][0]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(DatasetManifestError, match="checksum mismatch"):
        load_dataset_manifest(manifest_path)
