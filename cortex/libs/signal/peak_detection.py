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
from scipy.signal import find_peaks, lombscargle, welch


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


def compute_sdnn(ibi_ms: NDArray[np.float64]) -> float | None:
    """Compute SDNN (standard deviation of NN intervals) in milliseconds."""
    if len(ibi_ms) < 2:
        return None
    return float(np.std(ibi_ms, ddof=1))


def compute_pnn50(ibi_ms: NDArray[np.float64]) -> float | None:
    """Compute pNN50 as fraction of adjacent IBI diffs greater than 50ms."""
    if len(ibi_ms) < 2:
        return None
    diffs = np.abs(np.diff(ibi_ms))
    if diffs.size == 0:
        return None
    return float(np.mean(diffs > 50.0))


def compute_sd1_sd2(ibi_ms: NDArray[np.float64]) -> tuple[float | None, float | None]:
    """Compute Poincare plot SD1 and SD2 in milliseconds."""
    if len(ibi_ms) < 2:
        return None, None
    diffs = np.diff(ibi_ms)
    sd1 = float(np.sqrt(np.var(diffs, ddof=1) / 2.0)) if diffs.size >= 2 else None
    sdnn = compute_sdnn(ibi_ms)
    if sdnn is None or sd1 is None:
        return sd1, None
    # Based on SD2^2 = 2*SDNN^2 - 0.5*SD(diff)^2
    var_diffs = np.var(diffs, ddof=1) if diffs.size >= 2 else 0.0
    sd2_sq = max(0.0, 2.0 * (sdnn**2) - 0.5 * var_diffs)
    return sd1, float(np.sqrt(sd2_sq))


def compute_sample_entropy(
    series: NDArray[np.float64],
    m: int = 2,
    r_ratio: float = 0.2,
) -> float | None:
    """Compute sample entropy for a 1D series."""
    n = len(series)
    if n < m + 2:
        return None
    sd = np.std(series)
    if sd < 1e-9:
        return 0.0
    r = r_ratio * sd

    def _count_matches(order: int) -> int:
        count = 0
        for i in range(n - order):
            a = series[i : i + order]
            for j in range(i + 1, n - order + 1):
                b = series[j : j + order]
                if np.max(np.abs(a - b)) <= r:
                    count += 1
        return count

    b = _count_matches(m)
    a = _count_matches(m + 1)
    if b == 0 or a == 0:
        return None
    return float(-np.log(a / b))


def compute_lf_hf_ratio_lomb_scargle(
    ibi_ms: NDArray[np.float64],
) -> float | None:
    """
    Estimate LF/HF ratio via Lomb-Scargle on unevenly sampled IBI series.

    LF: 0.04-0.15Hz, HF: 0.15-0.40Hz
    """
    if len(ibi_ms) < 8:
        return None
    # Build beat-time axis in seconds using cumulative IBI.
    beat_t = np.cumsum(ibi_ms) / 1000.0
    beat_t = beat_t - beat_t[0]
    if beat_t[-1] < 20.0:
        return None

    rr = ibi_ms / 1000.0
    rr_centered = rr - np.mean(rr)
    freqs = np.linspace(0.04, 0.40, 256)
    ang = 2.0 * np.pi * freqs
    pgram = lombscargle(beat_t, rr_centered, ang, precenter=False, normalize=True)

    lf_mask = (freqs >= 0.04) & (freqs < 0.15)
    hf_mask = (freqs >= 0.15) & (freqs <= 0.40)
    lf = float(np.trapezoid(pgram[lf_mask], freqs[lf_mask])) if np.any(lf_mask) else 0.0
    hf = float(np.trapezoid(pgram[hf_mask], freqs[hf_mask])) if np.any(hf_mask) else 0.0
    if hf <= 1e-9:
        return None
    return float(lf / hf)


def compute_hrv_metrics(ibi_ms: NDArray[np.float64]) -> dict[str, float | None]:
    """Compute expanded HRV metric set from an IBI sequence."""
    rmssd = compute_rmssd(ibi_ms)
    sdnn = compute_sdnn(ibi_ms)
    pnn50 = compute_pnn50(ibi_ms)
    sd1, sd2 = compute_sd1_sd2(ibi_ms)
    lf_hf_ratio = compute_lf_hf_ratio_lomb_scargle(ibi_ms)
    sampen = compute_sample_entropy(ibi_ms)
    return {
        "rmssd": rmssd,
        "sdnn": sdnn,
        "pnn50": pnn50,
        "sd1": sd1,
        "sd2": sd2,
        "lf_hf_ratio": lf_hf_ratio,
        "sample_entropy": sampen,
    }


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


def compute_snr_db(
    bvp_signal: NDArray[np.float64],
    fs: float = 30.0,
    low_hz: float = 0.7,
    high_hz: float = 3.5,
) -> float:
    """Compute in-band to out-of-band SNR in dB."""
    if len(bvp_signal) < 4:
        return 0.0
    nperseg = min(len(bvp_signal), int(fs * 4))
    if nperseg < 4:
        return 0.0
    freqs, psd = welch(bvp_signal, fs=fs, nperseg=nperseg, noverlap=nperseg // 2)
    in_mask = (freqs >= low_hz) & (freqs <= high_hz)
    in_power = float(np.sum(psd[in_mask]))
    noise_power = float(np.sum(psd[~in_mask]))
    if in_power <= 1e-12:
        return -20.0
    if noise_power <= 1e-12:
        return 20.0
    return float(10.0 * np.log10(in_power / noise_power))


def compute_nsqi(
    bvp_signal: NDArray[np.float64],
    fs: float = 30.0,
    low_hz: float = 0.7,
    high_hz: float = 3.5,
) -> float:
    """
    Compute a normalized spectral quality index in [0, 1].

    HEURISTIC approximation aligned with published NSQI-style gating:
    combines in-band power ratio with peak concentration.
    """
    if len(bvp_signal) < 4:
        return 0.0
    nperseg = min(len(bvp_signal), int(fs * 4))
    freqs, psd = welch(bvp_signal, fs=fs, nperseg=nperseg, noverlap=max(1, nperseg // 2))
    total_power = float(np.sum(psd))
    if total_power <= 1e-12:
        return 0.0
    band_mask = (freqs >= low_hz) & (freqs <= high_hz)
    band_psd = psd[band_mask]
    band_freqs = freqs[band_mask]
    if band_psd.size == 0:
        return 0.0
    band_ratio = float(np.sum(band_psd) / total_power)
    peak_idx = int(np.argmax(band_psd))
    peak_freq = band_freqs[peak_idx]
    peak_window = np.abs(band_freqs - peak_freq) <= 0.15
    peak_concentration = float(np.sum(band_psd[peak_window]) / (np.sum(band_psd) + 1e-9))
    return float(np.clip(0.55 * band_ratio + 0.45 * peak_concentration, 0.0, 1.0))


def compute_physio_sqi(
    bvp_signal: NDArray[np.float64],
    *,
    fs: float = 30.0,
    low_hz: float = 0.7,
    high_hz: float = 3.5,
    motion_penalty: float = 0.0,
    face_presence_ratio: float = 1.0,
) -> tuple[float, dict[str, float]]:
    """
    Compute composite physiological SQI and its components.
    """
    nsqi = compute_nsqi(bvp_signal, fs=fs, low_hz=low_hz, high_hz=high_hz)
    snr_db = compute_snr_db(bvp_signal, fs=fs, low_hz=low_hz, high_hz=high_hz)
    snr_norm = float(np.clip((snr_db + 10.0) / 20.0, 0.0, 1.0))
    motion_term = float(np.clip(1.0 - motion_penalty, 0.0, 1.0))
    face_term = float(np.clip(face_presence_ratio, 0.0, 1.0))
    sqi = float(np.clip(0.45 * nsqi + 0.30 * snr_norm + 0.15 * motion_term + 0.10 * face_term, 0.0, 1.0))
    return sqi, {
        "nsqi": nsqi,
        "snr_db": snr_db,
        "snr_norm": snr_norm,
        "motion_term": motion_term,
        "face_presence_ratio": face_term,
    }
