"""Compatibility facade for research-only respiratory-rate estimation.

The production pipeline uses :mod:`cortex.services.physio_engine.v2.respiration`.
This facade retains a small signal-only API for offline experiments. It makes
no apnea or other medical classification and requires at least 30 seconds.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from cortex.services.physio_engine.v2.respiration import _spectral_channel


@dataclass(frozen=True)
class RespirationEstimate:
    """Research-only rate estimate from one respiratory proxy signal."""

    resp_rate_bpm: float | None
    confidence: float
    dominant_freq_hz: float | None
    unavailable_reason: str | None = None


class RespirationEstimator:
    """Legacy signal-only estimator with the v2 30-second evidence floor."""

    def __init__(
        self,
        fs: float = 30.0,
        low_hz: float = 0.08,
        high_hz: float = 0.50,
        filter_order: int = 3,
        **_deprecated: object,
    ) -> None:
        del filter_order
        self._fs = float(fs)
        self._low_hz = float(low_hz)
        self._high_hz = float(high_hz)
        self._latest: RespirationEstimate | None = None

    @property
    def latest_estimate(self) -> RespirationEstimate | None:
        return self._latest

    def set_fs(self, fs: float) -> None:
        if fs > 0:
            self._fs = float(fs)

    def process_bvp_window(
        self,
        bvp_window: NDArray[np.float64],
        blink_suppression: float = 0.0,
        motion_proxy_signal: NDArray[np.float64] | None = None,
        timestamp: float | None = None,
    ) -> RespirationEstimate:
        # Deprecated focus/timestamp arguments cannot turn this signal proxy
        # into a medical event. An explicit motion signal is preferred when
        # supplied; callers needing fusion use RespirationFusionV2.
        del blink_suppression, timestamp
        source = motion_proxy_signal if motion_proxy_signal is not None else bvp_window
        result = _spectral_channel(
            source,
            fs=self._fs,
            low_hz=self._low_hz,
            high_hz=self._high_hz,
            min_window_seconds=30.0,
        )
        if result is None:
            self._latest = RespirationEstimate(
                resp_rate_bpm=None,
                confidence=0.0,
                dominant_freq_hz=None,
                unavailable_reason="requires 30 seconds of periodic evidence",
            )
        else:
            self._latest = RespirationEstimate(
                resp_rate_bpm=result.rate_bpm,
                confidence=result.quality,
                dominant_freq_hz=result.rate_bpm / 60.0,
            )
        return self._latest

    def update_baseline(self, resp_baseline_bpm: float | None) -> None:
        del resp_baseline_bpm

    def reset(self) -> None:
        self._latest = None
