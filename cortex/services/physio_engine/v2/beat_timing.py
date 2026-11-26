"""Shared constants and noise model for narrow-band beat timing.

Beat instants feed pulse-rate variability, where timing error is squared:
independent jitter of sigma seconds on each beat adds ``6 * sigma**2`` to the
variance of successive interval differences.  Picking the three highest
samples of a 30 Hz, 0.7-3.5 Hz waveform puts sigma near 15 ms — roughly 35 ms
of fabricated RMSSD on a signal whose true RMSSD is often smaller.

The beat-to-beat information of a pulse wave lives in a narrow band around the
cardiac fundamental.  With intervals varying by +/- 70 ms at respiratory rates
up to 0.4 Hz, Carson's rule gives +/- 0.35 Hz; +/- 0.5 Hz leaves room for rate
drift inside one window.  Filtering to that band removes the out-of-band noise
that moves peaks around, and the analytic signal's phase then locates each
beat from the whole cycle instead of from its three highest samples.

What noise remains inside the band still moves the beats, by an amount that
follows from the carrier-to-noise ratio.  This module holds the filter that
both the pulse pipeline (to locate beats and estimate their timing error) and
the variability estimator (to remove that error from its statistics) must
agree on.
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np
from numpy.typing import NDArray
from scipy.integrate import trapezoid
from scipy.signal import sosfreqz

from cortex.libs.signal.filters import design_bandpass

BEAT_BAND_HALF_WIDTH_HZ = 0.5
BEAT_BAND_FILTER_ORDER = 2
# A beat whose narrow-band amplitude has collapsed relative to the window is
# a phase reading of noise.
BEAT_MIN_RELATIVE_AMPLITUDE = 0.30
# Noise density next to the cardiac line is read from the spectrum within this
# distance of it, outside the beat band and away from the harmonics.
BEAT_NOISE_SEARCH_HALF_WIDTH_HZ = 1.6
BEAT_NOISE_HARMONIC_GUARD_HZ = 0.25


def beat_band(
    cardiac_hz: float, *, low_hz: float, high_hz: float
) -> tuple[float, float] | None:
    """The beat filter's pass band for a cardiac line, or ``None`` if unusable."""

    low = max(low_hz, cardiac_hz - BEAT_BAND_HALF_WIDTH_HZ)
    high = min(high_hz, cardiac_hz + BEAT_BAND_HALF_WIDTH_HZ)
    if high - low < 0.2:
        return None
    return low, high


@lru_cache(maxsize=128)
def _band_power_response(
    low_hz: float, high_hz: float, sample_rate_hz: float
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    sos = design_bandpass(low_hz, high_hz, sample_rate_hz, BEAT_BAND_FILTER_ORDER)
    frequencies, response = sosfreqz(sos, worN=4096, fs=sample_rate_hz)
    # Forward-backward filtering squares the magnitude response, so noise
    # power passes through |H|^4.
    return (
        np.asarray(frequencies, dtype=np.float64),
        np.asarray(np.abs(response) ** 4, dtype=np.float64),
    )


def band_power_response(
    band: tuple[float, float], sample_rate_hz: float
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Frequencies and noise-power gain of the zero-phase beat filter."""

    return _band_power_response(
        round(band[0], 3), round(band[1], 3), round(sample_rate_hz, 2)
    )


def equivalent_noise_bandwidth_hz(
    band: tuple[float, float], sample_rate_hz: float
) -> float:
    frequencies, power = band_power_response(band, sample_rate_hz)
    return float(trapezoid(power, frequencies))


def timing_noise_correlation(
    lag_seconds: float,
    *,
    cardiac_hz: float,
    low_hz: float = 0.7,
    high_hz: float = 3.5,
    sample_rate_hz: float = 30.0,
) -> float:
    """Correlation of the timing error of two beats ``lag_seconds`` apart.

    The beat filter is about 1 Hz wide, so its output noise decorrelates over
    roughly one second: neighbouring beats share part of their timing error.
    Variability statistics difference neighbouring beats, so the shared part
    cancels and must not be subtracted as if it were independent jitter.
    """

    band = beat_band(cardiac_hz, low_hz=low_hz, high_hz=high_hz)
    if band is None:
        return 0.0
    frequencies, power = band_power_response(band, sample_rate_hz)
    total = float(trapezoid(power, frequencies))
    if total <= 1e-18:
        return 0.0
    weighted = float(
        trapezoid(
            power * np.cos(2.0 * np.pi * (frequencies - cardiac_hz) * lag_seconds),
            frequencies,
        )
    )
    return float(np.clip(weighted / total, -1.0, 1.0))
