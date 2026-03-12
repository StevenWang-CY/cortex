"""
Tests for Butterworth bandpass filter (libs/signal/filters.py).

Verifies:
- Passband signals are preserved (cardiac frequencies ~1 Hz)
- Stopband signals are attenuated (DC component, high-frequency noise)
- Edge cases: near-DC, near-Nyquist, minimum signal length
- Real-time filter state preservation across chunks
- Invalid parameter rejection
"""

from __future__ import annotations

import numpy as np
import pytest

from cortex.libs.signal.filters import (
    bandpass_filter,
    bandpass_filter_realtime,
    design_bandpass,
)


# =============================================================================
# Helper: generate a clean sinusoidal signal
# =============================================================================

def make_sinusoid(freq_hz: float, fs: float = 30.0, duration_s: float = 10.0) -> np.ndarray:
    """Generate a pure sinusoidal signal at the given frequency."""
    t = np.arange(0, duration_s, 1.0 / fs)
    return np.sin(2 * np.pi * freq_hz * t)


# =============================================================================
# Tests: design_bandpass
# =============================================================================


class TestDesignBandpass:
    """Tests for the filter design function."""

    def test_returns_sos_array(self) -> None:
        """SOS output should be a 2D array with 6 columns per section."""
        sos = design_bandpass(0.7, 3.5, 30.0, 4)
        assert sos.ndim == 2
        assert sos.shape[1] == 6  # SOS sections always have 6 coefficients

    def test_default_parameters(self) -> None:
        """Should work with default cardiac band parameters."""
        sos = design_bandpass()
        assert sos.shape[0] > 0  # At least one section

    def test_rejects_low_cutoff_zero(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            design_bandpass(low_hz=0.0)

    def test_rejects_low_cutoff_negative(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            design_bandpass(low_hz=-1.0)

    def test_rejects_high_cutoff_above_nyquist(self) -> None:
        """High cutoff at or above Nyquist should fail."""
        with pytest.raises(ValueError, match="Nyquist"):
            design_bandpass(high_hz=15.0, fs=30.0)  # Nyquist is 15 Hz

    def test_rejects_inverted_cutoffs(self) -> None:
        with pytest.raises(ValueError, match="less than"):
            design_bandpass(low_hz=3.0, high_hz=1.0)

    def test_rejects_equal_cutoffs(self) -> None:
        with pytest.raises(ValueError, match="less than"):
            design_bandpass(low_hz=2.0, high_hz=2.0)


# =============================================================================
# Tests: bandpass_filter (zero-phase, offline)
# =============================================================================


class TestBandpassFilter:
    """Tests for the zero-phase bandpass filter."""

    def test_passes_in_band_signal(self) -> None:
        """A 1 Hz sinusoid (60 BPM) should pass through with minimal attenuation."""
        sig = make_sinusoid(1.0, fs=30.0, duration_s=10.0)
        filtered = bandpass_filter(sig, low_hz=0.7, high_hz=3.5, fs=30.0)

        # Compare RMS of filtered vs original (should retain most power)
        # Allow some loss from filter roll-off at edges, but 1 Hz is well within band
        rms_original = np.sqrt(np.mean(sig**2))
        rms_filtered = np.sqrt(np.mean(filtered**2))
        ratio = rms_filtered / rms_original
        assert ratio > 0.85, f"In-band signal lost too much power: ratio={ratio:.3f}"

    def test_attenuates_dc_component(self) -> None:
        """DC offset (0 Hz) should be removed by the bandpass filter."""
        sig = make_sinusoid(1.0, fs=30.0, duration_s=10.0) + 5.0  # Add DC
        filtered = bandpass_filter(sig, low_hz=0.7, high_hz=3.5, fs=30.0)

        # DC should be eliminated — mean should be near zero
        assert abs(np.mean(filtered)) < 0.1, f"DC not removed: mean={np.mean(filtered):.3f}"

    def test_attenuates_high_frequency_noise(self) -> None:
        """Noise above the passband should be strongly attenuated."""
        fs = 30.0
        cardiac = make_sinusoid(1.0, fs=fs, duration_s=10.0)
        noise = 0.5 * make_sinusoid(10.0, fs=fs, duration_s=10.0)  # 10 Hz > 3.5 Hz cutoff
        sig = cardiac + noise

        filtered = bandpass_filter(sig, low_hz=0.7, high_hz=3.5, fs=fs)

        # Filtered should be close to the cardiac-only signal
        # Compute correlation — should be high
        corr = np.corrcoef(cardiac, filtered)[0, 1]
        assert corr > 0.95, f"High-freq noise not sufficiently attenuated: corr={corr:.3f}"

    def test_attenuates_below_passband(self) -> None:
        """A 0.2 Hz signal (below the 0.7 Hz cutoff) should be attenuated."""
        sig = make_sinusoid(0.2, fs=30.0, duration_s=20.0)
        filtered = bandpass_filter(sig, low_hz=0.7, high_hz=3.5, fs=30.0)

        rms_original = np.sqrt(np.mean(sig**2))
        rms_filtered = np.sqrt(np.mean(filtered**2))
        ratio = rms_filtered / rms_original
        assert ratio < 0.3, f"Below-band signal not attenuated: ratio={ratio:.3f}"

    def test_preserves_signal_length(self) -> None:
        """Output should have the same length as input."""
        sig = make_sinusoid(1.5, fs=30.0, duration_s=10.0)
        filtered = bandpass_filter(sig)
        assert len(filtered) == len(sig)

    def test_rejects_short_signal(self) -> None:
        """Signals shorter than minimum should raise ValueError."""
        short_sig = np.random.randn(5)
        with pytest.raises(ValueError, match="too short"):
            bandpass_filter(short_sig)

    def test_multiple_frequencies(self) -> None:
        """Mixed signal: in-band component preserved, out-of-band removed."""
        fs = 30.0
        # 1.5 Hz is in-band, 0.1 Hz and 8 Hz are out-of-band
        in_band = make_sinusoid(1.5, fs=fs, duration_s=10.0)
        out_low = 2.0 * make_sinusoid(0.1, fs=fs, duration_s=10.0)
        out_high = 1.0 * make_sinusoid(8.0, fs=fs, duration_s=10.0)
        sig = in_band + out_low + out_high

        filtered = bandpass_filter(sig, fs=fs)

        corr = np.corrcoef(in_band, filtered)[0, 1]
        assert corr > 0.90, f"Multi-freq filtering failed: corr={corr:.3f}"


# =============================================================================
# Tests: bandpass_filter_realtime (causal, streaming)
# =============================================================================


class TestBandpassFilterRealtime:
    """Tests for the causal real-time bandpass filter."""

    def test_basic_filtering(self) -> None:
        """Real-time filter should produce output of same length as input."""
        sos = design_bandpass()
        sig = make_sinusoid(1.0, fs=30.0, duration_s=5.0)
        filtered, _ = bandpass_filter_realtime(sig, sos)
        assert len(filtered) == len(sig)

    def test_state_continuity(self) -> None:
        """
        Processing in chunks with state should produce the same result
        as processing the whole signal at once.
        """
        sos = design_bandpass()
        sig = make_sinusoid(1.5, fs=30.0, duration_s=5.0)

        # Process whole signal
        whole_filtered, _ = bandpass_filter_realtime(sig, sos)

        # Process in chunks
        chunk_size = 30  # 1 second chunks
        zi = None
        chunks_filtered = []
        for i in range(0, len(sig), chunk_size):
            chunk = sig[i : i + chunk_size]
            filtered_chunk, zi = bandpass_filter_realtime(chunk, sos, zi)
            chunks_filtered.append(filtered_chunk)

        chunked_result = np.concatenate(chunks_filtered)

        # Should be identical (both are forward-only sosfilt)
        np.testing.assert_allclose(whole_filtered, chunked_result, atol=1e-10)

    def test_none_zi_initializes(self) -> None:
        """Passing zi=None should work without error."""
        sos = design_bandpass()
        sig = np.random.randn(100)
        filtered, zi_out = bandpass_filter_realtime(sig, sos, zi=None)
        assert filtered.shape == sig.shape
        assert zi_out is not None
