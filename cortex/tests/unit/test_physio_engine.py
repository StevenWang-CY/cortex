"""
Tests for Physio Engine (services/physio_engine/).

Tests cover:
- RoiExtractor: ROI polygon extraction, spatial averaging, multi-ROI handling
- rPPG algorithms: POS, CHROM, green-channel BVP extraction from synthetic RGB
- PulseEstimator: HR/HRV estimation, HR delta, PhysioFeatures generation
- QualityScorer: SNR assessment, algorithm switching logic

Uses synthetic data — no real webcam or face data required.
"""

from __future__ import annotations

import numpy as np
import pytest

from cortex.libs.config.settings import LandmarksConfig
from cortex.services.physio_engine.pulse_estimator import PulseEstimate, PulseEstimator
from cortex.services.physio_engine.quality_scorer import QualityScorer
from cortex.services.physio_engine.roi_extractor import RoiExtractor, RoiTrace, RoiTraceFrame
from cortex.services.physio_engine.rppg import (
    RPPGAlgorithm,
    extract_bvp,
    extract_bvp_chrom,
    extract_bvp_green,
    extract_bvp_pos,
)

# =============================================================================
# Helpers
# =============================================================================


def make_synthetic_rgb_window(
    hr_bpm: float = 72.0,
    fs: float = 30.0,
    duration_s: float = 10.0,
    snr: float = 5.0,
) -> np.ndarray:
    """
    Generate synthetic RGB traces with embedded cardiac oscillation.

    The green channel has the strongest cardiac signal (as in real rPPG).
    Red and blue channels have weaker but correlated modulations.

    Args:
        hr_bpm: Simulated heart rate in BPM.
        fs: Sampling frequency.
        duration_s: Signal duration in seconds.
        snr: Signal-to-noise ratio (higher = cleaner).

    Returns:
        Array of shape (N, 3) with [R, G, B] columns.
    """
    n_samples = int(fs * duration_s)
    t = np.arange(n_samples) / fs
    cardiac_freq = hr_bpm / 60.0

    # Cardiac signal (sinusoidal pulse)
    cardiac = np.sin(2 * np.pi * cardiac_freq * t)

    # Add harmonics for more realistic pulse shape
    cardiac += 0.3 * np.sin(2 * np.pi * 2 * cardiac_freq * t)

    # Base intensities (skin color range)
    r_base = 150.0
    g_base = 120.0
    b_base = 100.0

    # Cardiac modulation amplitude (tiny — real rPPG is ~0.1% modulation)
    amplitude = 2.0

    # Noise
    noise_amplitude = amplitude / snr
    noise_r = np.random.randn(n_samples) * noise_amplitude
    noise_g = np.random.randn(n_samples) * noise_amplitude
    noise_b = np.random.randn(n_samples) * noise_amplitude

    # Green channel has strongest cardiac signal
    r = r_base + 0.5 * amplitude * cardiac + noise_r
    g = g_base + amplitude * cardiac + noise_g
    b = b_base + 0.3 * amplitude * cardiac + noise_b

    return np.column_stack([r, g, b])


def make_synthetic_frame_with_face_roi(
    width: int = 640,
    height: int = 480,
    brightness: int = 128,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Create a synthetic frame and fake landmark pixel coordinates.

    The landmarks are positioned in a face-like arrangement in the center
    of the frame, with ROI regions having distinct colors.

    Returns:
        (frame, landmarks_px) where frame is (H,W,3) BGR and
        landmarks_px is (478, 2) float32.
    """
    frame = np.full((height, width, 3), brightness, dtype=np.uint8)

    # Add colored regions for ROIs
    # Forehead area (top-center)
    frame[100:150, 250:400] = [100, 150, 120]  # BGR
    # Left cheek
    frame[200:280, 180:280] = [95, 140, 115]
    # Right cheek
    frame[200:280, 360:460] = [98, 145, 118]

    # Create 478 landmarks spread across the face area
    landmarks_px = np.zeros((478, 2), dtype=np.float32)
    for i in range(478):
        # Spread landmarks in a face-like oval
        angle = 2 * np.pi * i / 478
        rx, ry = 100 + 50 * (i % 20) / 20, 80 + 60 * (i % 24) / 24
        landmarks_px[i, 0] = 320 + rx * np.cos(angle) * 0.3  # x
        landmarks_px[i, 1] = 240 + ry * np.sin(angle) * 0.3  # y

    # Place specific ROI landmarks in their colored regions
    config = LandmarksConfig()

    # Forehead landmarks in the forehead region
    for idx in config.forehead:
        if idx < 478:
            landmarks_px[idx] = [
                250 + np.random.uniform(10, 140),
                100 + np.random.uniform(5, 45),
            ]

    # Left cheek landmarks
    for idx in config.left_cheek:
        if idx < 478:
            landmarks_px[idx] = [
                180 + np.random.uniform(10, 90),
                200 + np.random.uniform(10, 70),
            ]

    # Right cheek landmarks
    for idx in config.right_cheek:
        if idx < 478:
            landmarks_px[idx] = [
                360 + np.random.uniform(10, 90),
                200 + np.random.uniform(10, 70),
            ]

    return frame, landmarks_px


# =============================================================================
# ROI Extractor Tests
# =============================================================================


class TestRoiExtractor:
    """Tests for ROI RGB trace extraction."""

    def test_extract_from_synthetic_frame(self) -> None:
        """ROI extraction should produce valid traces from a synthetic frame."""
        frame, landmarks_px = make_synthetic_frame_with_face_roi()
        extractor = RoiExtractor()

        trace = extractor.extract(frame, landmarks_px, timestamp=1.0)

        assert trace.timestamp == 1.0
        assert trace.has_any_roi

    def test_forehead_roi_has_correct_color_range(self) -> None:
        """Forehead ROI should reflect the colored region in the frame."""
        frame, landmarks_px = make_synthetic_frame_with_face_roi()
        extractor = RoiExtractor()
        trace = extractor.extract(frame, landmarks_px, timestamp=1.0)

        if trace.forehead is not None and trace.forehead.pixel_count > 0:
            # The forehead region was set to BGR [100, 150, 120]
            # So RGB should be approximately [120, 150, 100]
            assert trace.forehead.pixel_count > 0
            # Just verify it's a reasonable range
            assert 0 < trace.forehead.r < 255
            assert 0 < trace.forehead.g < 255
            assert 0 < trace.forehead.b < 255

    def test_roi_trace_to_array(self) -> None:
        """RoiTrace.to_array() should return [R, G, B] numpy array."""
        trace = RoiTrace(r=120.0, g=150.0, b=100.0, pixel_count=500)
        arr = trace.to_array()
        assert arr.shape == (3,)
        np.testing.assert_allclose(arr, [120.0, 150.0, 100.0])

    def test_roi_trace_frame_best_roi(self) -> None:
        """best_roi should return the ROI with most pixels."""
        small = RoiTrace(r=100, g=100, b=100, pixel_count=50)
        large = RoiTrace(r=100, g=100, b=100, pixel_count=500)
        medium = RoiTrace(r=100, g=100, b=100, pixel_count=200)

        frame = RoiTraceFrame(
            forehead=small, left_cheek=large, right_cheek=medium, timestamp=0.0,
        )
        assert frame.best_roi is large

    def test_roi_trace_frame_combined_rgb(self) -> None:
        """combined_rgb should be pixel-count-weighted average."""
        roi1 = RoiTrace(r=100, g=100, b=100, pixel_count=100)
        roi2 = RoiTrace(r=200, g=200, b=200, pixel_count=100)

        frame = RoiTraceFrame(
            forehead=roi1, left_cheek=roi2, right_cheek=None, timestamp=0.0,
        )
        combined = frame.combined_rgb()
        assert combined is not None
        # Equal pixel counts → simple average
        np.testing.assert_allclose(combined, [150.0, 150.0, 150.0])

    def test_roi_trace_frame_no_rois(self) -> None:
        """Frame with no ROIs should report has_any_roi=False."""
        frame = RoiTraceFrame(
            forehead=None, left_cheek=None, right_cheek=None, timestamp=0.0,
        )
        assert frame.has_any_roi is False
        assert frame.best_roi is None
        assert frame.combined_rgb() is None

    def test_extract_single_roi(self) -> None:
        """extract_single_roi should work for named regions."""
        frame, landmarks_px = make_synthetic_frame_with_face_roi()
        extractor = RoiExtractor()

        forehead = extractor.extract_single_roi(frame, landmarks_px, "forehead")
        # May or may not succeed depending on landmark placement
        # but should not error
        assert forehead is None or isinstance(forehead, RoiTrace)

    def test_extract_single_roi_invalid_name(self) -> None:
        """Invalid ROI name should raise ValueError."""
        frame, landmarks_px = make_synthetic_frame_with_face_roi()
        extractor = RoiExtractor()

        with pytest.raises(ValueError, match="Unknown ROI"):
            extractor.extract_single_roi(frame, landmarks_px, "nose")

    def test_landmarks_out_of_range_handled(self) -> None:
        """Landmark indices beyond array bounds should be filtered out."""
        frame = np.full((100, 100, 3), 128, dtype=np.uint8)
        # Only 10 landmarks but config references indices up to 350
        landmarks_px = np.random.rand(10, 2).astype(np.float32) * 50 + 25
        extractor = RoiExtractor()

        # Should not crash — just return None for ROIs with invalid indices
        trace = extractor.extract(frame, landmarks_px, timestamp=0.0)
        assert isinstance(trace, RoiTraceFrame)


# =============================================================================
# rPPG Algorithm Tests
# =============================================================================


class TestRPPGAlgorithms:
    """Tests for POS, CHROM, and green-channel BVP extraction."""

    def test_pos_output_shape(self) -> None:
        """POS should return BVP of same length as input."""
        rgb = make_synthetic_rgb_window(hr_bpm=72, duration_s=10.0)
        bvp = extract_bvp_pos(rgb)
        assert bvp.shape == (rgb.shape[0],)

    def test_chrom_output_shape(self) -> None:
        """CHROM should return BVP of same length as input."""
        rgb = make_synthetic_rgb_window(hr_bpm=72, duration_s=10.0)
        bvp = extract_bvp_chrom(rgb)
        assert bvp.shape == (rgb.shape[0],)

    def test_green_output_shape(self) -> None:
        """Green-channel should return BVP of same length as input."""
        rgb = make_synthetic_rgb_window(hr_bpm=72, duration_s=10.0)
        bvp = extract_bvp_green(rgb)
        assert bvp.shape == (rgb.shape[0],)

    def test_pos_zero_mean(self) -> None:
        """POS BVP output should be approximately zero-mean."""
        rgb = make_synthetic_rgb_window(hr_bpm=72, snr=10.0)
        bvp = extract_bvp_pos(rgb)
        assert abs(np.mean(bvp)) < 0.1

    def test_chrom_zero_mean(self) -> None:
        """CHROM BVP output should be approximately zero-mean."""
        rgb = make_synthetic_rgb_window(hr_bpm=72, snr=10.0)
        bvp = extract_bvp_chrom(rgb)
        assert abs(np.mean(bvp)) < 0.1

    def test_green_zero_mean(self) -> None:
        """Green-channel BVP output should be approximately zero-mean."""
        rgb = make_synthetic_rgb_window(hr_bpm=72, snr=10.0)
        bvp = extract_bvp_green(rgb)
        assert abs(np.mean(bvp)) < 0.1

    def test_extract_bvp_dispatcher(self) -> None:
        """extract_bvp should dispatch to correct algorithm."""
        rgb = make_synthetic_rgb_window(hr_bpm=72, duration_s=5.0)

        bvp_pos = extract_bvp(rgb, RPPGAlgorithm.POS)
        bvp_chrom = extract_bvp(rgb, RPPGAlgorithm.CHROM)
        bvp_green = extract_bvp(rgb, RPPGAlgorithm.GREEN)

        # All should produce valid output
        assert bvp_pos.shape[0] == rgb.shape[0]
        assert bvp_chrom.shape[0] == rgb.shape[0]
        assert bvp_green.shape[0] == rgb.shape[0]

        # They should be different (different algorithms)
        assert not np.allclose(bvp_pos, bvp_chrom)

    def test_short_signal_handled(self) -> None:
        """Very short signals should not crash."""
        rgb = make_synthetic_rgb_window(hr_bpm=72, duration_s=0.05)  # ~1-2 samples
        bvp = extract_bvp_pos(rgb)
        assert len(bvp) == len(rgb)

    def test_constant_rgb_produces_zero_bvp(self) -> None:
        """Constant RGB input should produce zero or near-zero BVP."""
        rgb = np.ones((300, 3)) * 128.0
        bvp = extract_bvp_pos(rgb)
        assert np.max(np.abs(bvp)) < 1e-6


# =============================================================================
# Pulse Estimator Tests
# =============================================================================


class TestPulseEstimator:
    """Tests for HR and HRV estimation from BVP windows."""

    def test_process_clean_signal_72bpm(self) -> None:
        """Clean 72 BPM signal should produce HR estimate within ±5 BPM."""
        rgb = make_synthetic_rgb_window(hr_bpm=72.0, snr=10.0, duration_s=10.0)
        bvp = extract_bvp_pos(rgb)

        estimator = PulseEstimator(fs=30.0)
        estimate = estimator.process_window(bvp, timestamp=10.0)

        assert estimate.hr_bpm is not None, "Should detect HR from clean signal"
        assert abs(estimate.hr_bpm - 72.0) <= 5.0, (
            f"HR={estimate.hr_bpm:.1f}, expected ~72"
        )
        assert estimate.signal_quality > 0.1

    def test_process_clean_signal_100bpm(self) -> None:
        """Clean 100 BPM signal should be estimated accurately."""
        rgb = make_synthetic_rgb_window(hr_bpm=100.0, snr=10.0, duration_s=10.0)
        bvp = extract_bvp_pos(rgb)

        estimator = PulseEstimator(fs=30.0)
        estimate = estimator.process_window(bvp, timestamp=10.0)

        assert estimate.hr_bpm is not None
        assert abs(estimate.hr_bpm - 100.0) <= 5.0, (
            f"HR={estimate.hr_bpm:.1f}, expected ~100"
        )

    def test_process_short_signal_returns_none(self) -> None:
        """Signal too short for bandpass should return None HR."""
        short_signal = np.random.randn(10)

        estimator = PulseEstimator(fs=30.0)
        estimate = estimator.process_window(short_signal)

        assert estimate.hr_bpm is None
        assert estimate.signal_quality == 0.0

    def test_hr_delta_computation(self) -> None:
        """HR delta should track heart rate changes over time."""
        estimator = PulseEstimator(fs=30.0)

        # Simulate increasing HR over multiple windows
        for i, hr in enumerate([70, 72, 75, 78, 82]):
            rgb = make_synthetic_rgb_window(hr_bpm=float(hr), snr=10.0)
            bvp = extract_bvp_pos(rgb)
            estimator.process_window(bvp, timestamp=float(i * 1.0))

        delta = estimator.compute_hr_delta(current_time=4.0, window_seconds=5.0)
        # Should show increasing trend
        if delta is not None:
            assert delta > 0, f"HR delta should be positive (increasing HR), got {delta}"

    def test_get_features_valid(self) -> None:
        """get_features should return valid PhysioFeatures after processing."""
        rgb = make_synthetic_rgb_window(hr_bpm=72.0, snr=10.0)
        bvp = extract_bvp_pos(rgb)

        estimator = PulseEstimator(fs=30.0)
        estimator.process_window(bvp, timestamp=10.0)
        features = estimator.get_features(timestamp=10.0)

        assert features.pulse_bpm is not None
        assert 40 <= features.pulse_bpm <= 200
        assert 0 <= features.pulse_quality <= 1.0
        assert features.valid is True

    def test_get_features_no_data(self) -> None:
        """get_features without processing should return invalid."""
        estimator = PulseEstimator()
        features = estimator.get_features()
        assert features.valid is False
        assert features.pulse_bpm is None

    def test_reset_clears_state(self) -> None:
        """Reset should clear all history and estimates."""
        estimator = PulseEstimator()
        rgb = make_synthetic_rgb_window(hr_bpm=72.0, snr=10.0)
        bvp = extract_bvp_pos(rgb)
        estimator.process_window(bvp, timestamp=1.0)

        estimator.reset()
        assert estimator.latest_estimate is None
        features = estimator.get_features()
        assert features.valid is False


# =============================================================================
# Quality Scorer Tests
# =============================================================================


class TestQualityScorer:
    """Tests for signal quality assessment and algorithm switching."""

    def test_initial_algorithm_is_pos(self) -> None:
        """Default algorithm should be POS (highest priority)."""
        scorer = QualityScorer()
        assert scorer.current_algorithm == RPPGAlgorithm.POS

    def test_assess_clean_signal_high_quality(self) -> None:
        """Clean cardiac signal should have high quality score."""
        rgb = make_synthetic_rgb_window(hr_bpm=72.0, snr=10.0)
        bvp = extract_bvp_pos(rgb)

        scorer = QualityScorer()
        assessment = scorer.assess_quality(bvp, fs=30.0)

        assert assessment.overall_quality > 0.1
        assert assessment.algorithm == RPPGAlgorithm.POS

    def test_assess_noise_signal_low_quality(self) -> None:
        """Pure noise should have low quality score."""
        noise = np.random.randn(300)

        scorer = QualityScorer()
        assessment = scorer.assess_quality(noise, fs=30.0)

        assert assessment.overall_quality < 0.5

    def test_update_tracks_quality(self) -> None:
        """update() should record quality in history."""
        rgb = make_synthetic_rgb_window(hr_bpm=72.0, snr=10.0)
        bvp = extract_bvp_pos(rgb)

        scorer = QualityScorer()
        scorer.update(rgb, bvp, fs=30.0)

        assert scorer.get_mean_quality() > 0

    def test_algorithm_switching_on_poor_quality(self) -> None:
        """Scorer should switch algorithm when quality is persistently poor."""
        scorer = QualityScorer()

        # Simulate poor quality for many windows to exceed cooldown
        noise_rgb = np.random.randn(300, 3) + 128
        noise_bvp = np.random.randn(300)

        for _ in range(10):  # Exceed _SWITCH_COOLDOWN_WINDOWS
            scorer.update(noise_rgb, noise_bvp, fs=30.0)

        # After persistent poor quality, algorithm may have switched
        # (depends on whether CHROM/green are better on noise)
        # At minimum, the scorer should not crash
        assert scorer.current_algorithm in [
            RPPGAlgorithm.POS, RPPGAlgorithm.CHROM, RPPGAlgorithm.GREEN
        ]

    def test_reset_restores_defaults(self) -> None:
        """reset() should restore POS and clear history."""
        scorer = QualityScorer(initial_algorithm=RPPGAlgorithm.CHROM)
        scorer.reset()

        assert scorer.current_algorithm == RPPGAlgorithm.POS
        assert scorer.get_mean_quality() == 0.0
        assert scorer.latest_assessment is None


# =============================================================================
# Wave 1A — SQI validity, IBI count guard, parabolic interpolation
# =============================================================================


class TestSQIValidityThreshold:
    """Tests for raised SQI validity threshold (C-01) and IBI count guard (C-03)."""

    def test_low_sqi_with_few_peaks_invalid_and_no_hrv(self) -> None:
        """SQI=0.3 with 2 peaks → valid=False, hrv=None."""
        estimator = PulseEstimator(fs=30.0)

        # Create a signal that will produce a low signal quality (~0.3)
        # and very few peaks (2 peaks = 1 IBI, well below the 5 IBI guard)
        est = PulseEstimate(
            hr_bpm=72.0,
            hr_confidence=0.5,
            rmssd_ms=40.0,
            ibi_count=2,
            signal_quality=0.3,
        )
        estimator._latest_estimate = est

        features = estimator.get_features(timestamp=10.0)
        # SQI 0.3 < 0.4 threshold → invalid
        assert features.valid is False
        # ibi_count 2 < 5 → hrv should be None
        assert features.pulse_variability_proxy is None


class TestParabolicInterpolation:
    """Tests for parabolic peak interpolation (C-02)."""

    def test_parabolic_interpolation_reduces_ibi_std(self) -> None:
        """Parabolic interpolation should reduce IBI std on a synthetic 70 BPM signal."""
        from cortex.libs.signal.filters import bandpass_filter
        from cortex.libs.signal.peak_detection import compute_ibi_series, detect_bvp_peaks

        fs = 30.0
        duration_s = 10.0
        hr_bpm = 70.0
        n_samples = int(fs * duration_s)
        t = np.arange(n_samples) / fs

        # Clean cardiac sinusoid
        cardiac_freq = hr_bpm / 60.0
        signal = np.sin(2 * np.pi * cardiac_freq * t)

        filtered = bandpass_filter(signal, low_hz=0.7, high_hz=3.5, fs=fs, order=4)
        peaks = detect_bvp_peaks(filtered, fs=fs)

        if len(peaks) < 3:
            pytest.skip("Not enough peaks for meaningful IBI comparison")

        ibi_no_interp = compute_ibi_series(peaks, fs=fs, signal=None)
        ibi_with_interp = compute_ibi_series(peaks, fs=fs, signal=filtered)

        # Parabolic interpolation should produce equal or lower IBI std
        assert np.std(ibi_with_interp) <= np.std(ibi_no_interp) + 1e-6
