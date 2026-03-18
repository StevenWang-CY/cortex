"""
BVP Peak Detection and Heart Rate Estimation

Implements Welch PSD-based heart rate estimation and inter-beat interval (IBI)
extraction from blood volume pulse (BVP) signals. Used by the physio engine
to derive instantaneous HR and RMSSD HRV metrics from rPPG signals.

Key operations:
- Welch PSD with configurable frequency resolution for HR estimation
- Time-domain peak detection for IBI series
- RMSSD computation from IBI series
- Dominant frequency extraction from PSD
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray
from scipy.signal import find_peaks, welch


def estimate_hr_welch(
    bvp_signal: NDArray[np.float64],
    fs: float = 30.0,
    freq_resolution: float = 0.1,
    low_hz: float = 0.7,
    high_hz: float = 3.5,
) -> tuple[float | None, float]:
    """
    Estimate heart rate from BVP signal using Welch's PSD method.

    Computes the power spectral density via Welch's method and finds the
    dominant frequency within the cardiac band. The frequency resolution
    determines the FFT segment length (nperseg = fs / freq_resolution).

    Args:
        bvp_signal: Bandpass-filtered BVP signal (1D).
        fs: Sampling frequency in Hz.
        freq_resolution: Desired frequency resolution in Hz.
        low_hz: Lower bound of cardiac frequency band in Hz.
        high_hz: Upper bound of cardiac frequency band in Hz.

    Returns:
        Tuple of (heart_rate_bpm, peak_power_ratio).
        heart_rate_bpm is None if no valid peak found.
        peak_power_ratio is the ratio of peak power to total power in band
        (useful as a quality/confidence indicator).
    """
    if len(bvp_signal) < 2:
        return None, 0.0

    # nperseg determines frequency resolution: resolution = fs / nperseg
    nperseg = int(fs / freq_resolution)
    # Clamp to signal length
    nperseg = min(nperseg, len(bvp_signal))

    if nperseg < 4:
        return None, 0.0

    freqs, psd = welch(bvp_signal, fs=fs, nperseg=nperseg, noverlap=nperseg // 2)

    # Restrict to cardiac band
    band_mask = (freqs >= low_hz) & (freqs <= high_hz)
    band_freqs = freqs[band_mask]
    band_psd = psd[band_mask]

    if len(band_psd) == 0:
        return None, 0.0

    total_band_power = np.sum(band_psd)
    if total_band_power <= 0:
        return None, 0.0

    # Find dominant frequency
    peak_idx = np.argmax(band_psd)
    dominant_freq = band_freqs[peak_idx]
    peak_power = band_psd[peak_idx]

    hr_bpm = dominant_freq * 60.0
    peak_power_ratio = float(peak_power / total_band_power)

    return float(hr_bpm), peak_power_ratio


def detect_bvp_peaks(
    bvp_signal: NDArray[np.float64],
    fs: float = 30.0,
    min_hr_bpm: float = 42.0,
    max_hr_bpm: float = 210.0,
) -> NDArray[np.intp]:
    """
    Detect peaks in a BVP signal for IBI extraction.

    Uses scipy.signal.find_peaks with distance constraints derived from
    the expected HR range. Minimum peak distance prevents detecting
    harmonics; maximum ensures we don't miss beats.

    Args:
        bvp_signal: Bandpass-filtered BVP signal (1D).
        fs: Sampling frequency in Hz.
        min_hr_bpm: Minimum expected heart rate (sets max peak distance).
        max_hr_bpm: Maximum expected heart rate (sets min peak distance).

    Returns:
        Array of peak indices into the signal.
    """
    if len(bvp_signal) < 3:
        return np.array([], dtype=np.intp)

    # Minimum distance between peaks (from max HR)
    min_distance_samples = int(fs * 60.0 / max_hr_bpm)
    min_distance_samples = max(1, min_distance_samples)

    # Use prominence to filter noise peaks
    signal_range = np.ptp(bvp_signal)
    if signal_range == 0:
        return np.array([], dtype=np.intp)

    min_prominence = signal_range * 0.1

    peaks, _ = find_peaks(
        bvp_signal,
        distance=min_distance_samples,
        prominence=min_prominence,
    )

    return peaks


def compute_ibi_series(
    peak_indices: NDArray[np.intp],
    fs: float = 30.0,
    signal: NDArray[np.float64] | None = None,
) -> NDArray[np.float64]:
    """
    Compute inter-beat interval (IBI) series from peak indices.

    If the original signal is provided, applies parabolic interpolation
    to refine peak locations to sub-sample precision before computing IBI.

    Args:
        peak_indices: Array of peak sample indices.
        fs: Sampling frequency in Hz.
        signal: Optional original signal for parabolic peak refinement.

    Returns:
        Array of inter-beat intervals in milliseconds.
    """
    if len(peak_indices) < 2:
        return np.array([], dtype=np.float64)

    # Apply parabolic interpolation for sub-sample peak refinement
    if signal is not None and len(signal) > 2:
        refined = []
        for pk in peak_indices:
            if 0 < pk < len(signal) - 1:
                a, b, c = signal[pk - 1], signal[pk], signal[pk + 1]
                denom = a - 2 * b + c
                offset = 0.5 * (a - c) / denom if abs(denom) > 1e-10 else 0.0
                refined.append(pk + offset)
            else:
                refined.append(float(pk))
        ibi_samples = np.diff(refined)
    else:
        # Convert sample differences to milliseconds
        ibi_samples = np.diff(peak_indices)

    ibi_ms = ibi_samples * (1000.0 / fs)

    return ibi_ms


def compute_rmssd(ibi_ms: NDArray[np.float64]) -> float | None:
    """
    Compute RMSSD (Root Mean Square of Successive Differences) from IBI series.

    RMSSD is a time-domain HRV metric that reflects parasympathetic
    (vagal) activity. Lower RMSSD indicates reduced HRV, which correlates
    with stress and cognitive overwhelm.

    Args:
        ibi_ms: Inter-beat intervals in milliseconds.

    Returns:
        RMSSD value in milliseconds, or None if insufficient data.
    """
    if len(ibi_ms) < 2:
        return None

    successive_diffs = np.diff(ibi_ms)
    rmssd = float(np.sqrt(np.mean(successive_diffs**2)))

    return rmssd


def compute_signal_quality(
    bvp_signal: NDArray[np.float64],
    fs: float = 30.0,
    low_hz: float = 0.7,
    high_hz: float = 3.5,
) -> float:
    """
    Compute signal quality as the ratio of in-band power to total power.

    A higher ratio indicates a cleaner cardiac signal with less noise.
    Used to gate algorithm switching (POS → CHROM → green-channel).

    Args:
        bvp_signal: Raw or minimally filtered BVP signal.
        fs: Sampling frequency in Hz.
        low_hz: Lower bound of cardiac band.
        high_hz: Upper bound of cardiac band.

    Returns:
        Signal quality score between 0.0 and 1.0.
    """
    if len(bvp_signal) < 4:
        return 0.0

    nperseg = min(len(bvp_signal), int(fs * 4))  # 4-second window max
    if nperseg < 4:
        return 0.0

    freqs, psd = welch(bvp_signal, fs=fs, nperseg=nperseg, noverlap=nperseg // 2)

    total_power = np.sum(psd)
    if total_power <= 0:
        return 0.0

    band_mask = (freqs >= low_hz) & (freqs <= high_hz)
    band_power = np.sum(psd[band_mask])

    quality = float(band_power / total_power)
    return min(1.0, max(0.0, quality))
