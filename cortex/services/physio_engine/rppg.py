"""
Physio Engine — rPPG Blood Volume Pulse Extraction

Implements three rPPG algorithms for extracting blood volume pulse (BVP)
signals from RGB face traces:

1. POS (Plane Orthogonal to Skin) — configured default
2. CHROM (Chrominance-Based) — classical alternative
3. Green-Channel — simple baseline reference

All algorithms consume RGB trace windows (10s at 30fps = 300 samples x 3 channels)
and produce a 1D BVP signal suitable for heart rate estimation.

References:
- POS: Wang et al., "Algorithmic Principles of Remote PPG" (2017)
- CHROM: de Haan & Jeanne, "Robust Pulse Rate from Chrominance-Based rPPG" (2013)
"""

from __future__ import annotations

from enum import StrEnum

import numpy as np
from numpy.typing import NDArray

from cortex.libs.signal.filters import bandpass_filter

# CHROM band-pass applied to the chrominance signals *before* the alpha
# tuning (de Haan & Jeanne 2013, Sec. III-C: alpha = sigma(Xf)/sigma(Yf) on
# the band-passed Xf, Yf).  The paper's band is 40-240 BPM (0.67-4 Hz).
CHROM_BANDPASS_LOW_HZ = 0.67
CHROM_BANDPASS_HIGH_HZ = 4.0
_CHROM_BANDPASS_ORDER = 3


class RPPGAlgorithm(StrEnum):
    """Available rPPG extraction algorithms."""
    POS = "pos"
    CHROM = "chrom"
    GREEN = "green"


def _normalize_backend(algorithm: RPPGAlgorithm | str) -> RPPGAlgorithm:
    if isinstance(algorithm, RPPGAlgorithm):
        return algorithm
    try:
        return RPPGAlgorithm(str(algorithm).lower())
    except ValueError as exc:
        supported = ", ".join(item.value for item in RPPGAlgorithm)
        raise ValueError(
            f"Unsupported rPPG backend {algorithm!r}; supported backends: {supported}"
        ) from exc


def extract_bvp_pos(
    rgb_window: NDArray[np.float64],
    fs: float = 30.0,
    window_length: int | None = None,
) -> NDArray[np.float64]:
    """
    POS (Plane Orthogonal to Skin) rPPG algorithm.

    Steps:
    1. Temporally normalize each channel by dividing by running mean
    2. Project onto chrominance axes S1 and S2
    3. Combine using adaptive ratio of standard deviations
    4. Apply overlap-add windowing for continuous BVP

    Args:
        rgb_window: RGB traces, shape (N, 3) where columns are [R, G, B].
                    Values are mean pixel intensities (0-255 range).
        fs: Sampling frequency in Hz.
        window_length: Sub-window length in samples for overlap-add
                      processing. When ``None`` (the default) it is derived
                      from ``fs`` as ~1.6 s — the interval specified in the
                      POS paper (Wang et al. 2017) — so the sub-window stays
                      time-correct off-30 fps instead of the old hardcoded
                      45 samples (which was 1.5 s only at exactly 30 fps and
                      stretched/shrank at any other rate).

    Returns:
        BVP signal of shape (N,).
    """
    if window_length is None:
        # POS single-window length ≈ 1.6 s (Wang 2017, §III). Clamp to a
        # sane floor so very low fs / short windows still process.
        window_length = max(8, int(round(1.6 * fs)))

    n_samples = rgb_window.shape[0]

    if n_samples < window_length:
        # Fall back to simple processing for short signals
        return _pos_single_window(rgb_window)

    # Overlap-add BVP reconstruction
    bvp = np.zeros(n_samples, dtype=np.float64)
    overlap_count = np.zeros(n_samples, dtype=np.float64)

    stride = window_length // 2  # 50% overlap
    for start in range(0, n_samples - window_length + 1, stride):
        end = start + window_length
        sub_window = rgb_window[start:end]

        sub_bvp = _pos_single_window(sub_window)

        # Apply Hanning window for smooth overlap-add
        hann = np.hanning(window_length)
        bvp[start:end] += sub_bvp * hann
        overlap_count[start:end] += hann

    # Normalize by overlap count
    nonzero = overlap_count > 0
    bvp[nonzero] /= overlap_count[nonzero]

    return bvp


def _pos_single_window(rgb_window: NDArray[np.float64]) -> NDArray[np.float64]:
    """
    Apply POS algorithm to a single temporal window.

    Args:
        rgb_window: Shape (N, 3) with [R, G, B] columns.

    Returns:
        BVP signal of shape (N,).
    """
    n = rgb_window.shape[0]
    if n < 2:
        return np.zeros(n, dtype=np.float64)

    # Step 1: Temporal normalization — divide by running mean
    # Use column-wise mean for the window
    mean_rgb = np.mean(rgb_window, axis=0, keepdims=True)
    mean_rgb = np.maximum(mean_rgb, 1e-6)  # Avoid division by zero
    normalized = rgb_window / mean_rgb

    # Step 2: Project onto chrominance axes
    # POS projection matrix: S1 and S2
    # S1 = G - B (green-blue difference)
    # S2 = G + B - 2*R (complement of red)
    s1 = normalized[:, 1] - normalized[:, 2]  # G - B
    s2 = normalized[:, 1] + normalized[:, 2] - 2.0 * normalized[:, 0]  # G + B - 2R

    # Step 3: Adaptive combination using standard deviation ratio
    std_s1 = np.std(s1)
    std_s2 = np.std(s2)

    if std_s2 < 1e-10:
        # S2 has no variance — use S1 only
        bvp = s1
    else:
        alpha = std_s1 / std_s2
        bvp = s1 + alpha * s2

    # Zero-mean the output
    bvp -= np.mean(bvp)

    return np.asarray(bvp, dtype=np.float64)


def _chrom_bandpass(
    signal: NDArray[np.float64],
    *,
    fs: float,
    low_hz: float,
    high_hz: float,
) -> NDArray[np.float64]:
    """Band-pass one chrominance signal; fall back to mean removal when the
    window is too short or the band is not representable at ``fs``.

    The fallback keeps CHROM total on tiny inputs (the registry contract) but
    is *not* the published algorithm; every production window (>= 8 s at
    >= 10 fps) takes the filtered path.
    """

    centred = np.asarray(signal, dtype=np.float64) - float(np.mean(signal))
    if fs <= 0 or not np.isfinite(fs):
        return centred
    high = min(float(high_hz), 0.45 * float(fs))
    if low_hz <= 0 or low_hz >= high:
        return centred
    try:
        return bandpass_filter(
            centred,
            low_hz=float(low_hz),
            high_hz=high,
            fs=float(fs),
            order=_CHROM_BANDPASS_ORDER,
        )
    except ValueError:
        return centred


def extract_bvp_chrom(
    rgb_window: NDArray[np.float64],
    fs: float = 30.0,
    low_hz: float = CHROM_BANDPASS_LOW_HZ,
    high_hz: float = CHROM_BANDPASS_HIGH_HZ,
) -> NDArray[np.float64]:
    """
    CHROM (Chrominance-Based) rPPG algorithm.

    Classical alternative using fixed chrominance projection coefficients.
    Cortex makes no comparative subgroup-performance claim until the
    subject-disjoint validation protocol is complete.

    Steps (de Haan & Jeanne 2013):
    1. Temporally normalize each channel
    2. Compute chrominance signals Xs and Ys
    3. Band-pass both to Xf and Yf
    4. Combine as S = Xf - alpha * Yf with alpha = sigma(Xf) / sigma(Yf)

    Earlier revisions computed alpha on the unfiltered Xs/Ys, which lets
    out-of-band illumination drift dominate the standard-deviation ratio.

    Args:
        rgb_window: RGB traces, shape (N, 3) with [R, G, B] columns.
        fs: Sampling frequency in Hz.
        low_hz: Lower edge of the internal chrominance band-pass.
        high_hz: Upper edge of the internal chrominance band-pass.

    Returns:
        BVP signal of shape (N,).
    """
    n = rgb_window.shape[0]
    if n < 2:
        return np.zeros(n, dtype=np.float64)

    # Temporal normalization
    mean_rgb = np.mean(rgb_window, axis=0, keepdims=True)
    mean_rgb = np.maximum(mean_rgb, 1e-6)
    normalized = rgb_window / mean_rgb

    r_n = normalized[:, 0]
    g_n = normalized[:, 1]
    b_n = normalized[:, 2]

    # CHROM chrominance signals
    # Xs = 3R - 2G (heavily weights red)
    # Ys = 1.5R + G - 1.5B
    xs = 3.0 * r_n - 2.0 * g_n
    ys = 1.5 * r_n + g_n - 1.5 * b_n

    # Band-pass before tuning alpha (Xf, Yf in the paper).
    xf = _chrom_bandpass(xs, fs=fs, low_hz=low_hz, high_hz=high_hz)
    yf = _chrom_bandpass(ys, fs=fs, low_hz=low_hz, high_hz=high_hz)

    # Combine using the standard deviation ratio of the *filtered* signals
    std_xf = np.std(xf)
    std_yf = np.std(yf)

    if std_yf < 1e-10:
        bvp = xf
    else:
        alpha = std_xf / std_yf
        bvp = xf - alpha * yf

    # Zero-mean
    bvp = bvp - np.mean(bvp)

    return np.asarray(bvp, dtype=np.float64)


def extract_bvp_green(
    rgb_window: NDArray[np.float64],
    fs: float = 30.0,
) -> NDArray[np.float64]:
    """
    Green-channel baseline rPPG method.

    Simplest approach: uses the green channel directly since hemoglobin
    absorption peaks near 540nm (green wavelength). Serves as a reference
    and an interpretable baseline. It is never selected silently.

    Args:
        rgb_window: RGB traces, shape (N, 3) with [R, G, B] columns.
        fs: Sampling frequency in Hz.

    Returns:
        BVP signal of shape (N,).
    """
    n = rgb_window.shape[0]
    if n < 2:
        return np.zeros(n, dtype=np.float64)

    # Green channel is column index 1
    green = rgb_window[:, 1].copy()

    # Temporal normalization (divide by mean)
    mean_g = np.mean(green)
    if mean_g < 1e-6:
        return np.zeros(n, dtype=np.float64)

    green = green / mean_g

    # Zero-mean
    green -= np.mean(green)

    return np.asarray(green, dtype=np.float64)


def extract_bvp(
    rgb_window: NDArray[np.float64],
    algorithm: RPPGAlgorithm | str = RPPGAlgorithm.POS,
    fs: float = 30.0,
) -> NDArray[np.float64]:
    """
    Extract BVP signal using the specified algorithm.

    Convenience wrapper that dispatches to the appropriate algorithm.

    Args:
        rgb_window: RGB traces, shape (N, 3).
        algorithm: Which rPPG algorithm to use.
        fs: Sampling frequency in Hz.

    Returns:
        BVP signal of shape (N,).
    """
    backend = _normalize_backend(algorithm)
    if backend == RPPGAlgorithm.POS:
        return extract_bvp_pos(rgb_window, fs)
    elif backend == RPPGAlgorithm.CHROM:
        return extract_bvp_chrom(rgb_window, fs)
    elif backend == RPPGAlgorithm.GREEN:
        return extract_bvp_green(rgb_window, fs)
    else:
        raise ValueError(f"Unknown algorithm: {backend}")
