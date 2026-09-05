"""Hardware-free regression tests for the pulse spectral estimator and gates.

Covers the signal-pipeline audit defects:

* D1  - the whole window enters the spectrum (no silent 8 s segmentation);
* D5  - heuristic bounds are labelled as such, statistical ones are not;
* D7  - the motion gate is driven by face-width motion and is reachable;
* D8  - CHROM tunes alpha on band-passed chrominance;
* D9  - the library HR estimate is not quantised to native bins;
* D13 - the HR prior ages instead of penalising true rate changes forever.
"""

from __future__ import annotations

from uuid import UUID

import numpy as np
import pytest
from scipy.signal import welch

from cortex.libs.schemas.physiology import BeatCandidate, EvidenceStatus, PhysiologyMetric
from cortex.libs.signal.filters import bandpass_filter
from cortex.libs.signal.peak_detection import estimate_hr_welch
from cortex.services.physio_engine.rppg import extract_bvp_chrom
from cortex.services.physio_engine.v2.backends import RPPGBackendRegistry
from cortex.services.physio_engine.v2.beats import BeatLedger
from cortex.services.physio_engine.v2.hrv import build_hrv_estimates
from cortex.services.physio_engine.v2.pulse import (
    PRIOR_HALF_LIFE_SECONDS,
    PRIOR_MAX_AGE_SECONDS,
    PulsePipelineV2,
    _spectral_hr,
    motion_penalty_from_face_widths,
    prior_weight,
)
from cortex.services.physio_engine.v2.respiration import RespirationFusionV2
from cortex.services.physio_engine.v2.uncertainty import (
    HEURISTIC_METHOD_PREFIX,
    interval_kind,
)

_BOOT = UUID("33333333-3333-3333-3333-333333333333")
FS = 30.0


def _times(seconds: float, fs: float = FS) -> np.ndarray:
    return np.arange(int(round(seconds * fs)), dtype=np.float64) / fs


def _mono(times_s: np.ndarray, offset_s: float = 0.0) -> np.ndarray:
    return np.rint((times_s + offset_s) * 1e9).astype(np.int64)


def _rgb_from_pulse(pulse: np.ndarray, drift: np.ndarray | None = None) -> np.ndarray:
    baseline = np.zeros_like(pulse) if drift is None else drift
    return np.column_stack(
        [
            100.0 + 0.4 * pulse + baseline,
            90.0 + 1.5 * pulse + 0.8 * baseline,
            80.0 + 0.2 * pulse + 1.2 * baseline,
        ]
    ).astype(np.float64)


def _pipeline(**kwargs: float) -> PulsePipelineV2:
    backend = RPPGBackendRegistry.with_packaged_backends().resolve("pos")
    return PulsePipelineV2(backend, **kwargs)


def _two_tone(times_s: np.ndarray) -> np.ndarray:
    """72 BPM tone plus a clearly stronger 100 BPM tone."""

    return np.sin(2 * np.pi * 1.2 * times_s) + 1.6 * np.sin(2 * np.pi * (100.0 / 60.0) * times_s)


# ---------------------------------------------------------------------------
# D1 - whole-window spectrum
# ---------------------------------------------------------------------------


class TestWholeWindowSpectrum:
    def test_tail_perturbation_changes_estimate_where_old_segmentation_ignored_it(self) -> None:
        t = _times(10.0)
        clean = bandpass_filter(np.sin(2 * np.pi * 1.2 * t), fs=FS)
        perturbed = clean.copy()
        # In-band burst confined to the last 2 s (applied after filtering so
        # the first 8 s are byte-identical between the two signals).
        perturbed[240:] += 4.0 * np.sin(2 * np.pi * (100.0 / 60.0) * t[240:])

        # The defect: nperseg = 8 s with 50 % overlap on a 10 s window is one
        # 240-sample segment, so the tail never entered the PSD.
        _, old_clean = welch(clean, fs=FS, nperseg=240, noverlap=120, nfft=2048)
        _, old_perturbed = welch(perturbed, fs=FS, nperseg=240, noverlap=120, nfft=2048)
        assert np.allclose(old_clean, old_perturbed)

        clean_peak = _spectral_hr(clean, fs=FS, low_hz=0.7, high_hz=3.5, prior_bpm=None)
        perturbed_peak = _spectral_hr(perturbed, fs=FS, low_hz=0.7, high_hz=3.5, prior_bpm=None)
        assert clean_peak.analysed_seconds == pytest.approx(10.0)
        assert perturbed_peak.analysed_seconds == pytest.approx(10.0)
        assert clean_peak.hr_bpm == pytest.approx(72.0, abs=0.5)
        # The burst is now evidence: peak concentration must drop.
        assert perturbed_peak.concentration < clean_peak.concentration - 0.05

    def test_window_length_sets_analysed_span_and_native_resolution(self) -> None:
        ten = bandpass_filter(np.sin(2 * np.pi * 1.2 * _times(10.0)), fs=FS)
        eight = bandpass_filter(np.sin(2 * np.pi * 1.2 * _times(8.0)), fs=FS)
        ten_peak = _spectral_hr(ten, fs=FS, low_hz=0.7, high_hz=3.5, prior_bpm=None)
        eight_peak = _spectral_hr(eight, fs=FS, low_hz=0.7, high_hz=3.5, prior_bpm=None)
        assert ten_peak.analysed_seconds == pytest.approx(10.0)
        assert eight_peak.analysed_seconds == pytest.approx(8.0)
        assert ten_peak.native_resolution_hz == pytest.approx(0.1)
        assert eight_peak.native_resolution_hz == pytest.approx(0.125)
        assert ten_peak.hr_bpm == pytest.approx(72.0, abs=0.5)
        assert eight_peak.hr_bpm == pytest.approx(72.0, abs=0.5)

    def test_pipeline_reports_true_analysed_span(self) -> None:
        t = _times(10.0)
        result = _pipeline().process_window(
            _rgb_from_pulse(np.sin(2 * np.pi * 1.2 * t)),
            _mono(t),
            sample_rate_hz=FS,
            boot_id=_BOOT,
            observation_quality=0.95,
        )
        assert result.spectral_analysed_seconds == pytest.approx(10.0)
        assert result.spectral_native_resolution_hz == pytest.approx(0.1)
        assert result.summary.hr.value == pytest.approx(72.0, abs=1.0)


# ---------------------------------------------------------------------------
# D13 - HR prior ageing
# ---------------------------------------------------------------------------


class TestPriorAgeing:
    def test_prior_weight_decays_with_documented_constants(self) -> None:
        assert prior_weight(None) == 0.0
        assert prior_weight(0.0) == pytest.approx(1.0)
        assert prior_weight(PRIOR_HALF_LIFE_SECONDS) == pytest.approx(0.5)
        assert prior_weight(2 * PRIOR_HALF_LIFE_SECONDS) == pytest.approx(0.25)
        assert prior_weight(PRIOR_MAX_AGE_SECONDS + 0.1) == 0.0

    def test_stale_prior_no_longer_penalises_a_true_rate_change(self) -> None:
        signal = bandpass_filter(_two_tone(_times(10.0)), fs=FS)

        def hr_at(age: float | None) -> float:
            peak = _spectral_hr(
                signal,
                fs=FS,
                low_hz=0.7,
                high_hz=3.5,
                prior_bpm=72.0,
                prior_age_seconds=age,
            )
            assert peak.hr_bpm is not None
            return peak.hr_bpm

        unbiased = _spectral_hr(signal, fs=FS, low_hz=0.7, high_hz=3.5, prior_bpm=None)
        assert unbiased.hr_bpm == pytest.approx(100.0, abs=1.0)
        # A fresh prior legitimately prefers continuity with the last estimate.
        assert hr_at(1.0) == pytest.approx(72.0, abs=1.0)
        # After a half-life the penalty is too weak to override the stronger peak.
        assert hr_at(PRIOR_HALF_LIFE_SECONDS) == pytest.approx(100.0, abs=1.0)
        assert hr_at(PRIOR_MAX_AGE_SECONDS + 5.0) == pytest.approx(100.0, abs=1.0)

    def test_pipeline_prior_age_is_measured_between_window_ends(self) -> None:
        t = _times(10.0)
        clean = _rgb_from_pulse(np.sin(2 * np.pi * 1.2 * t))
        two_tone = _rgb_from_pulse(_two_tone(t))

        fresh = _pipeline()
        first = fresh.process_window(
            clean, _mono(t), sample_rate_hz=FS, boot_id=_BOOT, observation_quality=0.95
        )
        assert first.summary.hr.value == pytest.approx(72.0, abs=1.0)
        assert fresh.prior_hr_bpm == pytest.approx(72.0, abs=1.0)
        overlapping = fresh.process_window(
            two_tone,
            _mono(t, offset_s=1.0),
            sample_rate_hz=FS,
            boot_id=_BOOT,
            observation_quality=0.95,
        )
        assert overlapping.prior_weight == pytest.approx(prior_weight(1.0), abs=1e-6)
        assert overlapping.summary.hr.value == pytest.approx(72.0, abs=1.0)

        stale = _pipeline()
        stale.process_window(
            clean, _mono(t), sample_rate_hz=FS, boot_id=_BOOT, observation_quality=0.95
        )
        much_later = stale.process_window(
            two_tone,
            _mono(t, offset_s=45.0),
            sample_rate_hz=FS,
            boot_id=_BOOT,
            observation_quality=0.95,
        )
        assert much_later.prior_weight == 0.0
        assert much_later.summary.hr.value == pytest.approx(100.0, abs=1.0)

    def test_reset_clears_prior_and_its_age(self) -> None:
        t = _times(10.0)
        pipeline = _pipeline()
        pipeline.process_window(
            _rgb_from_pulse(np.sin(2 * np.pi * 1.2 * t)),
            _mono(t),
            sample_rate_hz=FS,
            boot_id=_BOOT,
            observation_quality=0.95,
        )
        window_end_s = t[-1]
        assert pipeline.prior_age_seconds(int(11e9)) == pytest.approx(11.0 - window_end_s, abs=1e-6)
        pipeline.reset()
        assert pipeline.prior_hr_bpm is None
        assert pipeline.prior_age_seconds(int(11e9)) is None


# ---------------------------------------------------------------------------
# D9 - library HR estimate quantisation
# ---------------------------------------------------------------------------


class TestLibraryHrEstimate:
    @pytest.mark.parametrize("bpm", [61.0, 68.0, 73.5, 75.0])
    def test_estimate_is_not_quantised_to_native_bins(self, bpm: float) -> None:
        t = _times(10.0)
        ppg = np.sin(2 * np.pi * bpm / 60.0 * t) + 0.3 * np.sin(2 * np.pi * 2 * bpm / 60.0 * t)
        hr, ratio = estimate_hr_welch(ppg, fs=FS)
        assert hr is not None
        assert hr == pytest.approx(bpm, abs=0.5)
        assert 0.0 < ratio <= 1.0

    def test_native_grid_would_have_quantised_to_six_bpm(self) -> None:
        t = _times(10.0)
        ppg = np.sin(2 * np.pi * 73.5 / 60.0 * t)
        freqs, psd = welch(ppg, fs=FS, nperseg=300, noverlap=150)
        band = (freqs >= 0.7) & (freqs <= 3.5)
        native_bpm = float(freqs[band][int(np.argmax(psd[band]))] * 60.0)
        assert native_bpm == pytest.approx(72.0) or native_bpm == pytest.approx(78.0)
        hr, _ = estimate_hr_welch(ppg, fs=FS)
        assert hr is not None
        assert abs(hr - 73.5) < abs(native_bpm - 73.5)


# ---------------------------------------------------------------------------
# D5 - interval labelling
# ---------------------------------------------------------------------------


class TestHeuristicBoundLabels:
    def test_pulse_hr_bound_is_labelled_heuristic(self) -> None:
        t = _times(10.0)
        result = _pipeline().process_window(
            _rgb_from_pulse(np.sin(2 * np.pi * 1.2 * t)),
            _mono(t),
            sample_rate_hz=FS,
            boot_id=_BOOT,
            observation_quality=0.95,
        )
        uncertainty = result.summary.hr.uncertainty
        assert uncertainty is not None
        assert uncertainty.method.startswith(HEURISTIC_METHOD_PREFIX)
        assert uncertainty.confidence_level is None
        assert uncertainty.interval_kind == "heuristic"
        assert interval_kind(uncertainty) == "heuristic"
        assert uncertainty.lower <= result.summary.hr.value <= uncertainty.upper

    def test_respiration_bounds_are_labelled_heuristic(self) -> None:
        fs = 30.0
        t = np.arange(int(45 * fs), dtype=np.float64) / fs
        pulse = np.sin(2 * np.pi * 1.2 * t) * (1.0 + 0.30 * np.sin(2 * np.pi * 0.25 * t))
        backend = RPPGBackendRegistry.with_packaged_backends().resolve("pos")
        fusion = RespirationFusionV2(
            backend,
            minimum_channel_quality=0.20,
            experimental_publication_enabled=True,
        )
        result = fusion.process_window(
            _rgb_from_pulse(pulse),
            _mono(t, offset_s=1.0),
            sample_rate_hz=fs,
            boot_id=_BOOT,
            head_vertical_face_units=np.sin(2 * np.pi * 0.25 * t),
        )
        assert result.fused.status == EvidenceStatus.EXPERIMENTAL.value
        for estimate in (result.fused, *result.channels.values()):
            assert estimate.uncertainty is not None
            assert interval_kind(estimate.uncertainty) == "heuristic"
            assert estimate.uncertainty.confidence_level is None

    def test_bootstrap_hrv_interval_stays_statistical(self) -> None:
        ledger = BeatLedger(history_seconds=700.0)
        rng = np.random.default_rng(11)
        beat_ns = np.cumsum(rng.normal(1_000_000_000, 40_000_000, size=361)).astype(np.int64)
        candidates = [
            BeatCandidate(
                candidate_id=f"long-{index}",
                absolute_mono_ns=int(value),
                prominence=1.0,
                quality=0.9,
                source_window_id="long",
            )
            for index, value in enumerate(beat_ns)
        ]
        _, intervals = ledger.ingest(
            candidates,
            window_id="long",
            window_start_mono_ns=0,
            window_end_mono_ns=int(beat_ns[-1]) + 1_000_000_000,
            boundary_margin_ns=0,
        )
        identity = RPPGBackendRegistry.with_packaged_backends().resolve("pos").identity
        estimates = build_hrv_estimates(
            intervals, algorithm=identity, boot_id=_BOOT, enabled=True
        )
        rmssd = estimates[PhysiologyMetric.RMSSD]
        assert rmssd.status == EvidenceStatus.EXPERIMENTAL.value
        assert rmssd.uncertainty is not None
        assert interval_kind(rmssd.uncertainty) == "statistical"
        assert rmssd.uncertainty.confidence_level == pytest.approx(0.95)


# ---------------------------------------------------------------------------
# D7 - motion gate reachability
# ---------------------------------------------------------------------------


class TestMotionGate:
    def test_face_width_penalty_unit_and_scale(self) -> None:
        # 2 px/frame at 30 fps on a 160 px-wide face = 60 px/s / 160 px.
        realistic = 2.0 * 30.0 / 160.0
        assert realistic == pytest.approx(0.375)
        assert motion_penalty_from_face_widths(
            realistic, max_motion_face_widths_per_second=0.75
        ) == pytest.approx(0.5)
        assert motion_penalty_from_face_widths(None, max_motion_face_widths_per_second=0.75) == 0.0
        assert motion_penalty_from_face_widths(0.0, max_motion_face_widths_per_second=0.75) == 0.0
        assert motion_penalty_from_face_widths(5.0, max_motion_face_widths_per_second=0.75) == 1.0
        with pytest.raises(ValueError):
            motion_penalty_from_face_widths(0.1, max_motion_face_widths_per_second=0.0)

    def test_window_gate_is_reachable_with_realistic_motion(self) -> None:
        t = _times(10.0)
        rgb = _rgb_from_pulse(np.sin(2 * np.pi * 1.2 * t))

        def run(**motion: float | None):
            return _pipeline().process_window(
                rgb,
                _mono(t),
                sample_rate_hz=FS,
                boot_id=_BOOT,
                observation_quality=0.95,
                **motion,
            )

        still = run(motion_face_widths_per_second=0.05)
        moving = run(motion_face_widths_per_second=0.375)  # 2 px/frame @ 160 px face
        shaking = run(motion_face_widths_per_second=0.80)  # ~4.3 px/frame @ 160 px face

        assert still.summary.hr.value is not None
        assert moving.motion_penalty == pytest.approx(0.5)
        assert moving.summary.quality < still.summary.quality - 0.03
        assert shaking.summary.quality == 0.0
        assert shaking.summary.hr.value is None
        assert shaking.summary.hr.status == EvidenceStatus.REJECTED.value

        # The deprecated legacy proxy for the same 4 px/frame motion at 640 px
        # is 0.28 deg: below the 7.5 deg gate and a ~2 % SQI penalty at most.
        legacy = run(head_jitter_deg=4.0 * 45.0 / 640.0)
        assert legacy.summary.hr.value is not None
        assert legacy.summary.quality == pytest.approx(still.summary.quality, abs=0.02)

    def test_face_width_evidence_takes_precedence_over_legacy_proxy(self) -> None:
        t = _times(10.0)
        rgb = _rgb_from_pulse(np.sin(2 * np.pi * 1.2 * t))
        result = _pipeline().process_window(
            rgb,
            _mono(t),
            sample_rate_hz=FS,
            boot_id=_BOOT,
            observation_quality=0.95,
            motion_face_widths_per_second=0.05,
            head_jitter_deg=30.0,
        )
        assert result.summary.hr.value is not None
        assert result.motion_penalty == pytest.approx(0.05 / 0.75)

    def test_engine_threshold_is_recorded_in_algorithm_parameters(self) -> None:
        pipeline = _pipeline(max_motion_face_widths_per_second=0.5)
        t = _times(10.0)
        result = pipeline.process_window(
            _rgb_from_pulse(np.sin(2 * np.pi * 1.2 * t)),
            _mono(t),
            sample_rate_hz=FS,
            boot_id=_BOOT,
            observation_quality=0.95,
        )
        parameters = result.summary.algorithm.parameters
        assert parameters["max_motion_face_widths_per_second"] == 0.5
        assert parameters["prior_half_life_seconds"] == PRIOR_HALF_LIFE_SECONDS


# ---------------------------------------------------------------------------
# D8 - CHROM alpha on band-passed chrominance
# ---------------------------------------------------------------------------


def _legacy_chrom(rgb: np.ndarray) -> np.ndarray:
    mean = np.maximum(np.mean(rgb, axis=0, keepdims=True), 1e-6)
    normalized = rgb / mean
    xs = 3.0 * normalized[:, 0] - 2.0 * normalized[:, 1]
    ys = 1.5 * normalized[:, 0] + normalized[:, 1] - 1.5 * normalized[:, 2]
    alpha = np.std(xs) / np.std(ys)
    bvp = xs - alpha * ys
    return np.asarray(bvp - np.mean(bvp), dtype=np.float64)


def _in_band_ratio(signal: np.ndarray) -> float:
    freqs, psd = welch(signal, fs=FS, nperseg=len(signal), nfft=2048)
    band = (freqs >= 0.7) & (freqs <= 3.5)
    return float(np.sum(psd[band]) / np.sum(psd))


class TestChromBandpass:
    def test_alpha_is_tuned_on_bandpassed_chrominance(self) -> None:
        t = _times(10.0)
        pulse = np.sin(2 * np.pi * 1.2 * t)
        drift = 6.0 * np.sin(2 * np.pi * 0.05 * t)
        rgb = np.column_stack(
            [100.0 + 0.4 * pulse + drift, 90.0 + 1.5 * pulse + 0.6 * drift, 80.0 + 0.2 * pulse + 1.3 * drift]
        )
        legacy_ratio = _in_band_ratio(_legacy_chrom(rgb))
        current_ratio = _in_band_ratio(extract_bvp_chrom(rgb, FS))
        assert legacy_ratio < 0.8
        assert current_ratio > 0.95
        hr, _ = estimate_hr_welch(bandpass_filter(extract_bvp_chrom(rgb, FS), fs=FS), fs=FS)
        assert hr == pytest.approx(72.0, abs=1.0)

    def test_short_inputs_stay_finite_and_total(self) -> None:
        rgb = np.column_stack([np.linspace(100, 101, 5), np.linspace(90, 91, 5), np.linspace(80, 81, 5)])
        bvp = extract_bvp_chrom(rgb, FS)
        assert bvp.shape == (5,)
        assert np.isfinite(bvp).all()

    def test_registered_chrom_backend_is_versioned_for_the_change(self) -> None:
        registry = RPPGBackendRegistry.with_packaged_backends()
        chrom = registry.resolve("chrom")
        assert chrom.identity.version == "chrom/2.1.0"
        assert chrom.identity.version != registry.resolve("pos").identity.version
        t = _times(10.0)
        rgb = _rgb_from_pulse(np.sin(2 * np.pi * 1.2 * t))
        assert np.allclose(chrom.extract(rgb, fs=FS), extract_bvp_chrom(rgb, FS))
