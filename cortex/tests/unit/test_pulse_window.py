"""
Tests for peak detection, HR estimation, and sliding window manager.

Verifies:
- Welch PSD correctly identifies dominant cardiac frequency from synthetic PPG
- HR estimation accuracy within ±2 BPM for clean signals, ±5 BPM for noisy
- IBI extraction and RMSSD computation
- Signal quality scoring
- Sliding window buffer behavior (fill, stride, reset)
- Multi-channel window synchronization
"""

from __future__ import annotations

import numpy as np
import pytest

from cortex.libs.signal.filters import bandpass_filter
from cortex.libs.signal.peak_detection import (
    compute_ibi_series,
    compute_rmssd,
    compute_signal_quality,
    detect_bvp_peaks,
    estimate_hr_welch,
)
from cortex.libs.signal.windowing import (
    MultiChannelWindowManager,
    SlidingWindowManager,
    WindowConfig,
)


# =============================================================================
# Helper: generate synthetic PPG-like signals
# =============================================================================


def make_synthetic_ppg(
    hr_bpm: float = 72.0,
    fs: float = 30.0,
    duration_s: float = 10.0,
    noise_level: float = 0.0,
) -> np.ndarray:
    """
    Generate a synthetic PPG-like signal at a known heart rate.

    Creates a clean sinusoid at the cardiac frequency with optional
    additive white noise. The signal is already in the cardiac band.
    """
    freq_hz = hr_bpm / 60.0
    t = np.arange(0, duration_s, 1.0 / fs)
    # Fundamental + small harmonic for realism
    ppg = np.sin(2 * np.pi * freq_hz * t) + 0.3 * np.sin(2 * np.pi * 2 * freq_hz * t)
    if noise_level > 0:
        rng = np.random.default_rng(42)
        ppg += noise_level * rng.standard_normal(len(t))
    return ppg


# =============================================================================
# Tests: HR estimation via Welch PSD
# =============================================================================


class TestEstimateHRWelch:
    """Tests for Welch PSD-based heart rate estimation."""

    def test_clean_signal_72bpm(self) -> None:
        """Clean 72 BPM signal should be estimated within ±2 BPM."""
        ppg = make_synthetic_ppg(hr_bpm=72.0, fs=30.0, duration_s=10.0)
        hr, power_ratio = estimate_hr_welch(ppg, fs=30.0)
        assert hr is not None
        assert abs(hr - 72.0) < 2.0, f"HR estimate {hr:.1f} not within ±2 of 72 BPM"
        assert power_ratio > 0.0

    def test_clean_signal_100bpm(self) -> None:
        """Clean 100 BPM signal."""
        ppg = make_synthetic_ppg(hr_bpm=100.0, fs=30.0, duration_s=10.0)
        hr, _ = estimate_hr_welch(ppg, fs=30.0)
        assert hr is not None
        assert abs(hr - 100.0) <= 2.5, f"HR={hr:.1f}, expected ~100"

    def test_clean_signal_55bpm(self) -> None:
        """Clean 55 BPM signal (near lower bound)."""
        ppg = make_synthetic_ppg(hr_bpm=55.0, fs=30.0, duration_s=10.0)
        hr, _ = estimate_hr_welch(ppg, fs=30.0)
        assert hr is not None
        assert abs(hr - 55.0) < 3.0, f"HR={hr:.1f}, expected ~55"

    def test_noisy_signal_within_5bpm(self) -> None:
        """Noisy 72 BPM signal should still be within ±5 BPM."""
        ppg = make_synthetic_ppg(hr_bpm=72.0, fs=30.0, duration_s=10.0, noise_level=0.5)
        filtered = bandpass_filter(ppg, fs=30.0)
        hr, _ = estimate_hr_welch(filtered, fs=30.0)
        assert hr is not None
        assert abs(hr - 72.0) < 5.0, f"Noisy HR={hr:.1f}, expected ~72 within ±5"

    def test_empty_signal_returns_none(self) -> None:
        hr, power = estimate_hr_welch(np.array([]), fs=30.0)
        assert hr is None
        assert power == 0.0

    def test_very_short_signal_returns_none(self) -> None:
        hr, power = estimate_hr_welch(np.array([1.0]), fs=30.0)
        assert hr is None
        assert power == 0.0


# =============================================================================
# Tests: BVP peak detection
# =============================================================================


class TestDetectBVPPeaks:
    """Tests for time-domain peak detection."""

    def test_detects_peaks_in_clean_signal(self) -> None:
        """Should find approximately the right number of peaks for 72 BPM."""
        ppg = make_synthetic_ppg(hr_bpm=72.0, fs=30.0, duration_s=10.0)
        filtered = bandpass_filter(ppg, fs=30.0)
        peaks = detect_bvp_peaks(filtered, fs=30.0)

        # 72 BPM for 10s = ~12 beats
        expected_peaks = int(72.0 / 60.0 * 10.0)
        assert len(peaks) >= expected_peaks - 2
        assert len(peaks) <= expected_peaks + 2

    def test_empty_signal(self) -> None:
        peaks = detect_bvp_peaks(np.array([]), fs=30.0)
        assert len(peaks) == 0

    def test_flat_signal(self) -> None:
        """A flat signal should produce no peaks."""
        peaks = detect_bvp_peaks(np.ones(300), fs=30.0)
        assert len(peaks) == 0


# =============================================================================
# Tests: IBI and RMSSD
# =============================================================================


class TestIBIAndRMSSD:
    """Tests for inter-beat interval and RMSSD computation."""

    def test_ibi_from_regular_peaks(self) -> None:
        """Evenly spaced peaks at 72 BPM → IBI ~ 833 ms."""
        # 72 BPM = 1.2 Hz, period = ~25 samples at 30 fps
        peak_indices = np.arange(0, 300, 25, dtype=np.intp)
        ibi = compute_ibi_series(peak_indices, fs=30.0)
        expected_ibi_ms = (25 / 30.0) * 1000.0  # ~833.3 ms
        np.testing.assert_allclose(ibi, expected_ibi_ms, atol=1.0)

    def test_rmssd_regular_peaks(self) -> None:
        """Regular IBI series should have RMSSD near 0."""
        ibi = np.array([833.0, 833.0, 833.0, 833.0, 833.0])
        rmssd = compute_rmssd(ibi)
        assert rmssd is not None
        assert rmssd < 1.0  # Nearly zero for perfectly regular intervals

    def test_rmssd_variable_peaks(self) -> None:
        """Variable IBI should have non-trivial RMSSD."""
        ibi = np.array([800.0, 850.0, 780.0, 870.0, 820.0, 860.0])
        rmssd = compute_rmssd(ibi)
        assert rmssd is not None
        assert rmssd > 10.0  # Meaningful variability

    def test_rmssd_insufficient_data(self) -> None:
        assert compute_rmssd(np.array([800.0])) is None
        assert compute_rmssd(np.array([])) is None

    def test_ibi_insufficient_peaks(self) -> None:
        ibi = compute_ibi_series(np.array([100], dtype=np.intp), fs=30.0)
        assert len(ibi) == 0


# =============================================================================
# Tests: Signal quality
# =============================================================================


class TestSignalQuality:
    """Tests for signal quality scoring."""

    def test_clean_cardiac_signal_high_quality(self) -> None:
        """A clean cardiac sinusoid should have high in-band quality."""
        ppg = make_synthetic_ppg(hr_bpm=72.0, fs=30.0, duration_s=10.0, noise_level=0.0)
        quality = compute_signal_quality(ppg, fs=30.0)
        assert quality > 0.5, f"Clean signal quality too low: {quality:.3f}"

    def test_noise_only_low_quality(self) -> None:
        """Pure broadband noise should have low in-band quality."""
        rng = np.random.default_rng(42)
        noise = rng.standard_normal(300)
        quality = compute_signal_quality(noise, fs=30.0)
        # Noise power is spread across all frequencies, so in-band should be small
        assert quality < 0.5, f"Noise quality unexpectedly high: {quality:.3f}"

    def test_short_signal_zero_quality(self) -> None:
        quality = compute_signal_quality(np.array([1.0, 2.0]), fs=30.0)
        assert quality == 0.0


# =============================================================================
# Tests: SlidingWindowManager
# =============================================================================


class TestSlidingWindowManager:
    """Tests for the circular buffer sliding window."""

    def test_default_config(self) -> None:
        config = WindowConfig()
        assert config.window_samples == 300  # 10s * 30fps
        assert config.stride_samples == 30  # 1s * 30fps

    def test_buffer_fill_and_ready(self) -> None:
        """Window should become ready after filling + one stride."""
        config = WindowConfig(window_seconds=2.0, stride_seconds=0.5, fs=10.0)
        mgr = SlidingWindowManager(config)

        # Fill buffer (20 samples for 2s at 10Hz)
        for i in range(20):
            mgr.push(np.array([float(i)]))

        # Buffer is full but stride counter started from 0
        # 20 samples >= 5 (stride), so should be ready
        assert mgr.is_full
        assert mgr.window_ready()

    def test_window_shape_single_channel(self) -> None:
        config = WindowConfig(window_seconds=1.0, stride_seconds=0.5, fs=10.0)
        mgr = SlidingWindowManager(config)

        for i in range(10):
            mgr.push(np.array([float(i)]))

        window = mgr.get_window()
        assert window.shape == (10,)  # Single channel squeezed to 1D

    def test_window_shape_multi_channel(self) -> None:
        config = WindowConfig(window_seconds=1.0, stride_seconds=0.5, fs=10.0, n_channels=3)
        mgr = SlidingWindowManager(config)

        for i in range(10):
            mgr.push(np.array([float(i), float(i) * 2, float(i) * 3]))

        window = mgr.get_window()
        assert window.shape == (10, 3)

    def test_stride_resets_after_get(self) -> None:
        """After getting a window, need another stride to be ready again."""
        config = WindowConfig(window_seconds=1.0, stride_seconds=0.5, fs=10.0)
        mgr = SlidingWindowManager(config)

        for i in range(10):
            mgr.push(np.array([float(i)]))

        assert mgr.window_ready()
        mgr.get_window()
        assert not mgr.window_ready()

        # Push another stride worth
        for i in range(5):
            mgr.push(np.array([float(i)]))
        assert mgr.window_ready()

    def test_circular_buffer_drops_old(self) -> None:
        """Buffer should only keep window_samples most recent samples."""
        config = WindowConfig(window_seconds=1.0, stride_seconds=0.5, fs=10.0)
        mgr = SlidingWindowManager(config)

        # Push 20 samples into a 10-sample buffer
        for i in range(20):
            mgr.push(np.array([float(i)]))

        assert mgr.buffer_length == 10
        window = mgr.get_window()
        # Should contain samples 10-19 (most recent)
        expected = np.arange(10, 20, dtype=np.float64)
        np.testing.assert_array_equal(window, expected)

    def test_get_window_if_ready(self) -> None:
        config = WindowConfig(window_seconds=1.0, stride_seconds=0.5, fs=10.0)
        mgr = SlidingWindowManager(config)

        assert mgr.get_window_if_ready() is None

        for i in range(10):
            mgr.push(np.array([float(i)]))

        window = mgr.get_window_if_ready()
        assert window is not None
        assert len(window) == 10

    def test_push_chunk(self) -> None:
        config = WindowConfig(window_seconds=1.0, stride_seconds=0.5, fs=10.0)
        mgr = SlidingWindowManager(config)
        chunk = np.arange(10, dtype=np.float64)
        mgr.push_chunk(chunk)
        assert mgr.buffer_length == 10
        assert mgr.window_ready()

    def test_reset(self) -> None:
        config = WindowConfig(window_seconds=1.0, stride_seconds=0.5, fs=10.0)
        mgr = SlidingWindowManager(config)

        for i in range(10):
            mgr.push(np.array([float(i)]))

        mgr.reset()
        assert mgr.buffer_length == 0
        assert mgr.total_samples == 0
        assert not mgr.is_full

    def test_raises_on_get_empty(self) -> None:
        mgr = SlidingWindowManager()
        with pytest.raises(RuntimeError, match="not full"):
            mgr.get_window()


# =============================================================================
# Tests: MultiChannelWindowManager
# =============================================================================


class TestMultiChannelWindowManager:
    """Tests for synchronized multi-channel windowing."""

    def test_push_all_channels(self) -> None:
        config = WindowConfig(window_seconds=1.0, stride_seconds=0.5, fs=10.0)
        mgr = MultiChannelWindowManager(["R", "G", "B"], config)

        for i in range(10):
            mgr.push_all({"R": float(i), "G": float(i * 2), "B": float(i * 3)})

        assert mgr.all_windows_ready()

    def test_get_stacked_window(self) -> None:
        config = WindowConfig(window_seconds=1.0, stride_seconds=0.5, fs=10.0)
        mgr = MultiChannelWindowManager(["R", "G", "B"], config)

        for i in range(10):
            mgr.push_all({"R": float(i), "G": float(i * 2), "B": float(i * 3)})

        stacked = mgr.get_stacked_window()
        assert stacked.shape == (10, 3)
        # Verify channel ordering matches channel_names
        np.testing.assert_array_equal(stacked[:, 0], np.arange(10, dtype=np.float64))
        np.testing.assert_array_equal(stacked[:, 1], np.arange(10, dtype=np.float64) * 2)
        np.testing.assert_array_equal(stacked[:, 2], np.arange(10, dtype=np.float64) * 3)

    def test_unknown_channel_raises(self) -> None:
        mgr = MultiChannelWindowManager(["R", "G", "B"])
        with pytest.raises(KeyError, match="Unknown channel"):
            mgr.push("X", 1.0)

    def test_reset_all_channels(self) -> None:
        config = WindowConfig(window_seconds=1.0, stride_seconds=0.5, fs=10.0)
        mgr = MultiChannelWindowManager(["R", "G", "B"], config)

        for i in range(10):
            mgr.push_all({"R": float(i), "G": float(i * 2), "B": float(i * 3)})

        mgr.reset()
        assert not mgr.all_windows_ready()
        assert not mgr.any_window_ready()
