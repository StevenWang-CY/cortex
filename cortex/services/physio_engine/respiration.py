"""
Physio Engine — Respiration Estimator (Screen Apnea Detection)

Extracts respiratory rate from the BVP (Blood Volume Pulse) signal by
isolating the respiratory modulation using a Butterworth bandpass filter
in the 0.15–0.4 Hz range (9–24 breaths/min).

Screen apnea is detected when:
- respiration_rate < 8 breaths/min AND
- visual_focus is high (blink suppression indicating fixation)

References:
- Respiratory modulation of rPPG is well-documented in the BVP envelope
- Screen apnea: Linda Stone, "Continuous Partial Attention" (2008)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from cortex.libs.signal.filters import bandpass_filter

logger = logging.getLogger(__name__)

# Respiratory frequency band
_RESP_LOW_HZ = 0.15   # ~9 breaths/min
_RESP_HIGH_HZ = 0.40  # ~24 breaths/min
_RESP_FILTER_ORDER = 4

# Apnea detection thresholds
_APNEA_RESP_THRESHOLD = 8.0  # breaths/min
_APNEA_BLINK_SUPPRESSION_THRESHOLD = 0.5  # high visual focus


@dataclass(frozen=True)
class RespirationEstimate:
    """Result of respiratory rate estimation from BVP signal."""

    resp_rate_bpm: float | None  # Breaths per minute
    confidence: float  # 0-1, based on spectral peak prominence
    apnea_detected: bool  # True if resp_rate < threshold with high visual focus
    dominant_freq_hz: float | None  # Peak respiratory frequency


class RespirationEstimator:
    """
    Extracts respiratory rate from BVP signal windows.

    The BVP signal contains respiratory modulation as amplitude/frequency
    variation. We isolate this by bandpass filtering at 0.15–0.4 Hz and
    finding the dominant frequency via Welch PSD.

    Usage:
        estimator = RespirationEstimator(fs=30.0)
        estimate = estimator.process_bvp_window(bvp, blink_suppression=0.7)
    """

    def __init__(
        self,
        fs: float = 30.0,
        low_hz: float = _RESP_LOW_HZ,
        high_hz: float = _RESP_HIGH_HZ,
        filter_order: int = _RESP_FILTER_ORDER,
        apnea_resp_threshold: float = _APNEA_RESP_THRESHOLD,
        apnea_blink_threshold: float = _APNEA_BLINK_SUPPRESSION_THRESHOLD,
    ) -> None:
        self._fs = fs
        self._low_hz = low_hz
        self._high_hz = high_hz
        self._filter_order = filter_order
        self._apnea_resp_threshold = apnea_resp_threshold
        self._apnea_blink_threshold = apnea_blink_threshold
        self._latest: RespirationEstimate | None = None

    @property
    def latest_estimate(self) -> RespirationEstimate | None:
        """Get the most recent respiration estimate."""
        return self._latest

    def process_bvp_window(
        self,
        bvp_window: NDArray[np.float64],
        blink_suppression: float = 0.0,
    ) -> RespirationEstimate:
        """
        Extract respiratory rate from a BVP signal window.

        Steps:
        1. Apply Butterworth bandpass filter (0.15–0.4 Hz)
        2. Compute Welch PSD in the respiratory band
        3. Find dominant frequency → convert to breaths/min
        4. Assess confidence from spectral peak prominence
        5. Check apnea conditions

        Args:
            bvp_window: Raw BVP signal, shape (N,). Should be ~10 seconds at 30fps.
            blink_suppression: Current blink suppression score (0-1). High = fixating.

        Returns:
            RespirationEstimate with rate, confidence, and apnea flag.
        """
        from scipy.signal import welch

        n_samples = len(bvp_window)
        min_filter_samples = 3 * (2 * self._filter_order + 1)

        if n_samples < min_filter_samples:
            logger.debug("BVP window too short for respiratory filtering: %d samples", n_samples)
            return self._empty_estimate()

        # Step 1: Bandpass filter for respiratory band
        try:
            resp_signal = bandpass_filter(
                bvp_window,
                low_hz=self._low_hz,
                high_hz=self._high_hz,
                fs=self._fs,
                order=self._filter_order,
            )
        except ValueError:
            logger.debug("Respiratory bandpass filter failed")
            return self._empty_estimate()

        # Step 2: Welch PSD in respiratory band
        # Use nperseg = min(256, n_samples) for good frequency resolution
        nperseg = min(256, n_samples)
        try:
            freqs, psd = welch(
                resp_signal,
                fs=self._fs,
                nperseg=nperseg,
                noverlap=nperseg // 2,
            )
        except Exception:
            logger.debug("Welch PSD failed for respiratory signal")
            return self._empty_estimate()

        # Step 3: Find peak in respiratory band
        resp_mask = (freqs >= self._low_hz) & (freqs <= self._high_hz)
        if not np.any(resp_mask):
            return self._empty_estimate()

        resp_freqs = freqs[resp_mask]
        resp_psd = psd[resp_mask]

        peak_idx = np.argmax(resp_psd)
        dominant_freq = float(resp_freqs[peak_idx])
        peak_power = float(resp_psd[peak_idx])

        # Convert frequency to breaths per minute
        resp_rate_bpm = dominant_freq * 60.0

        # Step 4: Confidence from spectral peak prominence
        # Ratio of peak power to total power in respiratory band
        total_power = float(np.sum(resp_psd))
        if total_power < 1e-12:
            return self._empty_estimate()

        # Also consider ratio to full-band power for quality
        full_total = float(np.sum(psd))
        in_band_ratio = total_power / full_total if full_total > 1e-12 else 0.0
        peak_prominence = peak_power / total_power

        # Confidence combines peak prominence and in-band power ratio
        confidence = float(np.clip(
            0.6 * peak_prominence + 0.4 * in_band_ratio,
            0.0, 1.0,
        ))

        # Step 5: Check apnea conditions
        apnea_detected = (
            resp_rate_bpm < self._apnea_resp_threshold
            and blink_suppression >= self._apnea_blink_threshold
            and confidence > 0.3  # Don't flag on noisy signals
        )

        estimate = RespirationEstimate(
            resp_rate_bpm=resp_rate_bpm,
            confidence=confidence,
            apnea_detected=apnea_detected,
            dominant_freq_hz=dominant_freq,
        )
        self._latest = estimate
        return estimate

    def _empty_estimate(self) -> RespirationEstimate:
        """Return an empty estimate when processing fails."""
        estimate = RespirationEstimate(
            resp_rate_bpm=None,
            confidence=0.0,
            apnea_detected=False,
            dominant_freq_hz=None,
        )
        self._latest = estimate
        return estimate

    def reset(self) -> None:
        """Reset estimator state."""
        self._latest = None
