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
import time
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
        apnea_sustain_seconds: float = 30.0,
        resp_baseline_bpm: float | None = None,
    ) -> None:
        self._fs = fs
        self._low_hz = low_hz
        self._high_hz = high_hz
        self._filter_order = filter_order
        self._apnea_resp_threshold = apnea_resp_threshold
        self._apnea_blink_threshold = apnea_blink_threshold
        self._apnea_sustain_seconds = apnea_sustain_seconds
        self._resp_baseline_bpm = resp_baseline_bpm
        self._latest: RespirationEstimate | None = None
        self._low_resp_started_at: float | None = None

    @property
    def latest_estimate(self) -> RespirationEstimate | None:
        """Get the most recent respiration estimate."""
        return self._latest

    def process_bvp_window(
        self,
        bvp_window: NDArray[np.float64],
        blink_suppression: float = 0.0,
        motion_proxy_signal: NDArray[np.float64] | None = None,
        timestamp: float | None = None,
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

        bvp_rate, bvp_conf, bvp_freq = self._estimate_rate_from_signal(bvp_window)
        motion_rate, motion_conf, motion_freq = self._estimate_rate_from_signal(motion_proxy_signal)

        if bvp_rate is None and motion_rate is None:
            return self._empty_estimate()

        if motion_rate is None or motion_conf <= 0.0:
            resp_rate_bpm = bvp_rate
            confidence = bvp_conf
            dominant_freq = bvp_freq
        elif bvp_rate is None or bvp_conf <= 0.0:
            resp_rate_bpm = motion_rate
            confidence = motion_conf
            dominant_freq = motion_freq
        else:
            total_w = bvp_conf + motion_conf
            if total_w <= 1e-9:
                resp_rate_bpm = bvp_rate
                confidence = bvp_conf
                dominant_freq = bvp_freq
            else:
                resp_rate_bpm = (bvp_rate * bvp_conf + motion_rate * motion_conf) / total_w
                confidence = float(np.clip(total_w / 2.0, 0.0, 1.0))
                dominant_freq = (resp_rate_bpm or 0.0) / 60.0

        now = timestamp if timestamp is not None else time.monotonic()
        personal_threshold = self._apnea_resp_threshold
        if self._resp_baseline_bpm is not None and self._resp_baseline_bpm > 0:
            personal_threshold = min(personal_threshold, 0.5 * self._resp_baseline_bpm)

        apnea_detected = False
        if (
            resp_rate_bpm is not None
            and resp_rate_bpm < personal_threshold
            and blink_suppression >= self._apnea_blink_threshold
            and confidence > 0.3
        ):
            if self._low_resp_started_at is None:
                self._low_resp_started_at = now
            apnea_detected = (now - self._low_resp_started_at) >= self._apnea_sustain_seconds
        else:
            self._low_resp_started_at = None

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

    def _estimate_rate_from_signal(
        self,
        signal: NDArray[np.float64] | None,
    ) -> tuple[float | None, float, float | None]:
        """Estimate respiration rate and confidence from one signal path."""
        if signal is None or len(signal) < 4:
            return None, 0.0, None
        from scipy.signal import welch

        try:
            resp_signal = bandpass_filter(
                signal,
                low_hz=self._low_hz,
                high_hz=self._high_hz,
                fs=self._fs,
                order=self._filter_order,
            )
        except ValueError:
            return None, 0.0, None

        nperseg = min(256, len(resp_signal))
        try:
            freqs, psd = welch(resp_signal, fs=self._fs, nperseg=nperseg, noverlap=nperseg // 2)
        except Exception:
            return None, 0.0, None

        resp_mask = (freqs >= self._low_hz) & (freqs <= self._high_hz)
        if not np.any(resp_mask):
            return None, 0.0, None
        resp_freqs = freqs[resp_mask]
        resp_psd = psd[resp_mask]
        peak_idx = int(np.argmax(resp_psd))
        dominant_freq = float(resp_freqs[peak_idx])
        peak_power = float(resp_psd[peak_idx])
        total_power = float(np.sum(resp_psd))
        if total_power <= 1e-12:
            return None, 0.0, None
        full_total = float(np.sum(psd))
        in_band_ratio = total_power / full_total if full_total > 1e-12 else 0.0
        peak_prominence = peak_power / total_power
        confidence = float(np.clip(0.6 * peak_prominence + 0.4 * in_band_ratio, 0.0, 1.0))
        return dominant_freq * 60.0, confidence, dominant_freq

    def update_baseline(self, resp_baseline_bpm: float | None) -> None:
        """Update personalized respiratory baseline."""
        self._resp_baseline_bpm = resp_baseline_bpm

    def reset(self) -> None:
        """Reset estimator state."""
        self._latest = None
        self._low_resp_started_at = None
