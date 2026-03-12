"""
Butterworth Bandpass Filter for rPPG Signal Processing

Implements configurable Butterworth bandpass filtering used to isolate the
cardiac frequency band (default 0.7–3.5 Hz, corresponding to 42–210 BPM)
from raw rPPG blood volume pulse (BVP) signals.

Uses scipy.signal's second-order sections (SOS) representation for
numerical stability, which is critical for real-time streaming applications.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray
from scipy.signal import butter, sosfilt, sosfiltfilt


def design_bandpass(
    low_hz: float = 0.7,
    high_hz: float = 3.5,
    fs: float = 30.0,
    order: int = 4,
) -> NDArray[np.float64]:
    """
    Design a Butterworth bandpass filter in SOS format.

    Args:
        low_hz: Lower cutoff frequency in Hz.
        high_hz: Upper cutoff frequency in Hz.
        fs: Sampling frequency in Hz.
        order: Filter order. The actual filter order for a bandpass is 2*order.

    Returns:
        Second-order sections (SOS) representation of the filter.

    Raises:
        ValueError: If frequencies are invalid relative to Nyquist.
    """
    nyquist = fs / 2.0

    if low_hz <= 0:
        raise ValueError(f"Low cutoff must be positive, got {low_hz}")
    if high_hz >= nyquist:
        raise ValueError(
            f"High cutoff ({high_hz} Hz) must be below Nyquist ({nyquist} Hz)"
        )
    if low_hz >= high_hz:
        raise ValueError(
            f"Low cutoff ({low_hz} Hz) must be less than high cutoff ({high_hz} Hz)"
        )

    sos = butter(order, [low_hz / nyquist, high_hz / nyquist], btype="band", output="sos")
    return sos


def bandpass_filter(
    signal: NDArray[np.float64],
    low_hz: float = 0.7,
    high_hz: float = 3.5,
    fs: float = 30.0,
    order: int = 4,
) -> NDArray[np.float64]:
    """
    Apply a Butterworth bandpass filter to a signal (zero-phase, offline).

    Uses forward-backward filtering (sosfiltfilt) for zero phase distortion.
    Best for offline/batch processing where the full signal is available.

    Args:
        signal: 1D input signal array.
        low_hz: Lower cutoff frequency in Hz.
        high_hz: Upper cutoff frequency in Hz.
        fs: Sampling frequency in Hz.
        order: Filter order.

    Returns:
        Filtered signal with same shape as input.

    Raises:
        ValueError: If signal is too short for the filter.
    """
    sos = design_bandpass(low_hz, high_hz, fs, order)

    # sosfiltfilt needs at least 3 * max(len(section)) samples per section
    # For a bandpass of given order, the minimum is roughly 3 * (2 * order + 1)
    min_samples = 3 * (2 * order + 1)
    if len(signal) < min_samples:
        raise ValueError(
            f"Signal length ({len(signal)}) too short for filter order {order}. "
            f"Need at least {min_samples} samples."
        )

    return sosfiltfilt(sos, signal)


def bandpass_filter_realtime(
    signal: NDArray[np.float64],
    sos: NDArray[np.float64],
    zi: NDArray[np.float64] | None = None,
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """
    Apply a causal Butterworth bandpass filter for real-time streaming.

    Uses forward-only filtering (sosfilt) with state preservation across
    calls for continuous real-time processing. Introduces phase delay
    but allows sample-by-sample or chunk-by-chunk processing.

    Args:
        signal: 1D input signal chunk.
        sos: Second-order sections from design_bandpass().
        zi: Filter state from previous call. None initializes to zeros.

    Returns:
        Tuple of (filtered_signal, new_filter_state).
    """
    if zi is None:
        # Initialize filter state to zeros — shape is (n_sections, 2)
        n_sections = sos.shape[0]
        zi = np.zeros((n_sections, 2))

    filtered, zi_out = sosfilt(sos, signal, zi=zi)
    return filtered, zi_out
