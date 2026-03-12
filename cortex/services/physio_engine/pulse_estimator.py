"""
Physio Engine — Pulse Estimator

Consumes 10-second BVP windows and produces physiological features:
- Instantaneous heart rate (BPM) via Welch PSD peak detection
- IBI series → RMSSD (HRV proxy)
- HR delta (5-second gradient for trend detection)

Bridges the signal processing library (libs/signal/) with the physio engine
by applying bandpass filtering and then delegating to the peak detection
and HRV computation utilities.

Output rate per spec:
- HR updated every 1 second (each stride)
- HRV updated every 5 seconds (requires longer window for stable IBI)
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

from cortex.libs.schemas.features import PhysioFeatures
from cortex.libs.signal.filters import bandpass_filter
from cortex.libs.signal.peak_detection import (
    compute_ibi_series,
    compute_rmssd,
    compute_signal_quality,
    detect_bvp_peaks,
    estimate_hr_welch,
)

logger = logging.getLogger(__name__)

# Default signal parameters
_DEFAULT_FS = 30.0
_DEFAULT_LOW_HZ = 0.7
_DEFAULT_HIGH_HZ = 3.5
_DEFAULT_FILTER_ORDER = 4


@dataclass(frozen=True)
class PulseEstimate:
    """Raw pulse estimation from a single BVP window."""

    hr_bpm: float | None  # Instantaneous heart rate
    hr_confidence: float  # PSD peak power ratio (0-1)
    rmssd_ms: float | None  # HRV proxy in milliseconds
    ibi_count: int  # Number of inter-beat intervals detected
    signal_quality: float  # In-band power ratio (0-1)


class PulseEstimator:
    """
    Estimates heart rate and HRV from BVP signal windows.

    Maintains a rolling history of HR estimates for computing the
    5-second HR delta (trend detection for state scoring).

    Usage:
        estimator = PulseEstimator()
        estimate = estimator.process_window(bvp_window)
        features = estimator.get_features()
    """

    def __init__(
        self,
        fs: float = _DEFAULT_FS,
        low_hz: float = _DEFAULT_LOW_HZ,
        high_hz: float = _DEFAULT_HIGH_HZ,
        filter_order: int = _DEFAULT_FILTER_ORDER,
        hr_history_seconds: float = 10.0,
    ) -> None:
        self._fs = fs
        self._low_hz = low_hz
        self._high_hz = high_hz
        self._filter_order = filter_order

        # Rolling HR history for delta computation
        # Store (timestamp, hr_bpm) pairs
        self._hr_history: deque[tuple[float, float]] = deque(
            maxlen=int(hr_history_seconds * 2)  # ~2 entries per second
        )

        # Latest estimates
        self._latest_estimate: PulseEstimate | None = None
        self._latest_timestamp: float = 0.0

    @property
    def latest_estimate(self) -> PulseEstimate | None:
        """Get the most recent pulse estimate."""
        return self._latest_estimate

    def process_window(
        self,
        bvp_window: NDArray[np.float64],
        timestamp: float = 0.0,
    ) -> PulseEstimate:
        """
        Process a BVP signal window to estimate HR and HRV.

        Steps:
        1. Apply Butterworth bandpass filter (0.7–3.5 Hz)
        2. Estimate HR via Welch PSD
        3. Detect peaks for IBI extraction
        4. Compute RMSSD from IBI series
        5. Score signal quality

        Args:
            bvp_window: Raw BVP signal, shape (N,). Should be ~10 seconds.
            timestamp: Timestamp of the window center.

        Returns:
            PulseEstimate with HR, HRV, and quality metrics.
        """
        n_samples = len(bvp_window)
        min_filter_samples = 3 * (2 * self._filter_order + 1)

        # Check minimum length for filtering
        if n_samples < min_filter_samples:
            logger.debug(f"BVP window too short for filtering: {n_samples} samples")
            return PulseEstimate(
                hr_bpm=None, hr_confidence=0.0,
                rmssd_ms=None, ibi_count=0, signal_quality=0.0,
            )

        # Step 1: Bandpass filter
        try:
            filtered = bandpass_filter(
                bvp_window,
                low_hz=self._low_hz,
                high_hz=self._high_hz,
                fs=self._fs,
                order=self._filter_order,
            )
        except ValueError:
            logger.debug("Bandpass filter failed on BVP window")
            return PulseEstimate(
                hr_bpm=None, hr_confidence=0.0,
                rmssd_ms=None, ibi_count=0, signal_quality=0.0,
            )

        # Step 2: HR estimation via Welch PSD
        hr_bpm, hr_confidence = estimate_hr_welch(
            filtered, fs=self._fs, low_hz=self._low_hz, high_hz=self._high_hz,
        )

        # Step 3: Peak detection for IBI
        peaks = detect_bvp_peaks(filtered, fs=self._fs)

        # Step 4: IBI and RMSSD
        ibi = compute_ibi_series(peaks, fs=self._fs)
        rmssd = compute_rmssd(ibi)
        ibi_count = len(ibi)

        # Step 5: Signal quality
        quality = compute_signal_quality(
            bvp_window, fs=self._fs, low_hz=self._low_hz, high_hz=self._high_hz,
        )

        estimate = PulseEstimate(
            hr_bpm=hr_bpm,
            hr_confidence=hr_confidence,
            rmssd_ms=rmssd,
            ibi_count=ibi_count,
            signal_quality=quality,
        )

        # Update history
        if hr_bpm is not None:
            self._hr_history.append((timestamp, hr_bpm))
        self._latest_estimate = estimate
        self._latest_timestamp = timestamp

        return estimate

    def compute_hr_delta(self, current_time: float, window_seconds: float = 5.0) -> float | None:
        """
        Compute heart rate change over the last N seconds.

        Uses linear regression over the HR history to compute the gradient.

        Args:
            current_time: Current timestamp.
            window_seconds: Look-back window in seconds.

        Returns:
            HR delta in BPM/window, or None if insufficient data.
        """
        if len(self._hr_history) < 2:
            return None

        cutoff = current_time - window_seconds
        recent = [(t, hr) for t, hr in self._hr_history if t >= cutoff]

        if len(recent) < 2:
            return None

        times = np.array([t for t, _ in recent])
        hrs = np.array([hr for _, hr in recent])

        # Simple delta: latest - earliest in window
        hr_delta = float(hrs[-1] - hrs[0])
        return hr_delta

    def get_features(self, timestamp: float = 0.0) -> PhysioFeatures:
        """
        Build PhysioFeatures from the latest estimate.

        Args:
            timestamp: Current timestamp for HR delta computation.

        Returns:
            PhysioFeatures Pydantic model.
        """
        est = self._latest_estimate

        if est is None or est.hr_bpm is None:
            return PhysioFeatures(
                pulse_bpm=None,
                pulse_quality=0.0,
                pulse_variability_proxy=None,
                hr_delta_5s=None,
                valid=False,
            )

        hr_delta = self.compute_hr_delta(timestamp)

        return PhysioFeatures(
            pulse_bpm=est.hr_bpm,
            pulse_quality=est.signal_quality,
            pulse_variability_proxy=est.rmssd_ms,
            hr_delta_5s=hr_delta,
            valid=est.signal_quality > 0.1,
        )

    def reset(self) -> None:
        """Reset all state."""
        self._hr_history.clear()
        self._latest_estimate = None
        self._latest_timestamp = 0.0
