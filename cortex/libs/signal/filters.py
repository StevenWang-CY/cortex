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
from scipy.signal import butter, sosfilt, sosfilt_zi, sosfiltfilt


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
    return np.asarray(sos, dtype=np.float64)


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

    return np.asarray(sosfiltfilt(sos, signal), dtype=np.float64)


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
        zi: Filter state from previous call. None initializes the state to
            the filter's steady-state response scaled by the first sample
            (see below).

    Returns:
        Tuple of (filtered_signal, new_filter_state).
    """
    if zi is None:
        # P2-8: seed the state with the filter's steady-state response
        # (sosfilt_zi) scaled by the first input sample, rather than zeros.
        # A zero initial state forces the filter to ramp up from 0 to the
        # signal's operating point over the first ~order samples, injecting
        # a large transient at the start of every stream. Scaling the
        # steady-state ``zi`` by the first sample starts the filter already
        # settled at the incoming DC level, so the transient disappears.
        # An empty chunk has no first sample, so fall back to zeros.
        steady = sosfilt_zi(sos)
        if signal.size > 0:
            zi = steady * float(signal[0])
        else:
            zi = np.zeros_like(steady)

    # ``sosfilt`` cannot reshape a zero-length input, so short-circuit an
    # empty chunk: there is nothing to filter and the state is unchanged.
    if signal.size == 0:
        return (
            np.asarray(signal, dtype=np.float64),
            np.asarray(zi, dtype=np.float64),
        )

    filtered, zi_out = sosfilt(sos, signal, zi=zi)
    return (
        np.asarray(filtered, dtype=np.float64),
        np.asarray(zi_out, dtype=np.float64),
    )
