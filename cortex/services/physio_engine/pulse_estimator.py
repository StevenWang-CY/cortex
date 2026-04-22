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
    compute_hrv_metrics,
    compute_ibi_series,
    compute_physio_sqi,
    compute_signal_quality,
    detect_bvp_peaks,
    estimate_hr_welch,
)
from cortex.services.physio_engine.respiration import RespirationEstimator

logger = logging.getLogger(__name__)

# Default signal parameters
_DEFAULT_FS = 30.0
_DEFAULT_LOW_HZ = 0.7
_DEFAULT_HIGH_HZ = 3.5
_DEFAULT_FILTER_ORDER = 4


@dataclass(frozen=True)
class PulseEstimate:
    """Raw pulse estimation from a single BVP window."""

    hr_bpm: float | None = None  # Instantaneous heart rate
    hr_confidence: float = 0.0  # PSD peak power ratio (0-1)
    rmssd_ms: float | None = None  # HRV proxy in milliseconds
    sdnn_ms: float | None = None
    pnn50: float | None = None
    sd1_ms: float | None = None
    sd2_ms: float | None = None
    lf_hf_ratio: float | None = None
    sample_entropy: float | None = None
    ibi_count: int = 0  # Number of inter-beat intervals detected
    signal_quality: float = 0.0  # In-band power ratio (0-1)
    physio_sqi: float = 0.0
    sqi_components: dict[str, float] = field(default_factory=dict)


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
        nsqi_threshold: float = 0.293,
        min_cardiac_snr_db: float = 2.0,
        hrv_min_window_seconds: float = 60.0,
        hrv_min_valid_ibi: int = 30,
    ) -> None:
        self._fs = fs
        self._low_hz = low_hz
        self._high_hz = high_hz
        self._filter_order = filter_order
        self._nsqi_threshold = nsqi_threshold
        self._min_cardiac_snr_db = min_cardiac_snr_db
        self._hrv_min_window_seconds = hrv_min_window_seconds
        self._hrv_min_valid_ibi = hrv_min_valid_ibi

        # Rolling HR history for delta computation
        # Store (timestamp, hr_bpm) pairs
        self._hr_history: deque[tuple[float, float]] = deque(
            maxlen=int(hr_history_seconds * 2)  # ~2 entries per second
        )

        # Latest estimates
        self._latest_estimate: PulseEstimate | None = None
        self._latest_timestamp: float = 0.0
        self._ibi_history: deque[tuple[float, float]] = deque(maxlen=2000)

        # Respiration estimator (runs alongside cardiac estimation)
        self._resp_estimator = RespirationEstimator(fs=fs)

    @property
    def latest_estimate(self) -> PulseEstimate | None:
        """Get the most recent pulse estimate."""
        return self._latest_estimate

    def process_window(
        self,
        bvp_window: NDArray[np.float64],
        timestamp: float = 0.0,
        *,
        head_jitter_deg: float = 0.0,
        face_presence_ratio: float = 1.0,
        motion_resp_signal: NDArray[np.float64] | None = None,
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
                rmssd_ms=None, sdnn_ms=None, pnn50=None, sd1_ms=None, sd2_ms=None,
                lf_hf_ratio=None, sample_entropy=None, ibi_count=0, signal_quality=0.0,
                physio_sqi=0.0, sqi_components={},
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
                rmssd_ms=None, sdnn_ms=None, pnn50=None, sd1_ms=None, sd2_ms=None,
                lf_hf_ratio=None, sample_entropy=None, ibi_count=0, signal_quality=0.0,
                physio_sqi=0.0, sqi_components={},
            )

        # Step 2: HR estimation via Welch PSD
        hr_bpm, hr_confidence = estimate_hr_welch(
            filtered, fs=self._fs, low_hz=self._low_hz, high_hz=self._high_hz,
        )

        # Step 3: Peak detection for IBI
        peaks = detect_bvp_peaks(filtered, fs=self._fs)

        # Step 4: IBI and expanded HRV metrics (with parabolic peak interpolation)
        ibi = compute_ibi_series(peaks, fs=self._fs, signal=filtered)
        self._update_ibi_history(ibi, timestamp)
        rolling_ibi = self._get_rolling_ibi(timestamp)
        hrv = (
            compute_hrv_metrics(rolling_ibi)
            if rolling_ibi.size >= self._hrv_min_valid_ibi
            else {
                "rmssd": None,
                "sdnn": None,
                "pnn50": None,
                "sd1": None,
                "sd2": None,
                "lf_hf_ratio": None,
                "sample_entropy": None,
            }
        )
        ibi_count = len(ibi)

        # Step 5: Signal quality and composite SQI.
        quality = compute_signal_quality(
            bvp_window, fs=self._fs, low_hz=self._low_hz, high_hz=self._high_hz,
        )
        motion_penalty = float(np.clip(head_jitter_deg / 15.0, 0.0, 1.0))
        physio_sqi, sqi_components = compute_physio_sqi(
            bvp_window,
            fs=self._fs,
            low_hz=self._low_hz,
            high_hz=self._high_hz,
            motion_penalty=motion_penalty,
            face_presence_ratio=face_presence_ratio,
        )
        sqi_components["head_jitter_deg"] = float(head_jitter_deg)
        sqi_components["nsqi_threshold"] = float(self._nsqi_threshold)

        estimate = PulseEstimate(
            hr_bpm=hr_bpm,
            hr_confidence=hr_confidence,
            rmssd_ms=hrv["rmssd"],
            sdnn_ms=hrv["sdnn"],
            pnn50=hrv["pnn50"],
            sd1_ms=hrv["sd1"],
            sd2_ms=hrv["sd2"],
            lf_hf_ratio=hrv["lf_hf_ratio"],
            sample_entropy=hrv["sample_entropy"],
            ibi_count=ibi_count,
            signal_quality=quality,
            physio_sqi=physio_sqi,
            sqi_components=sqi_components,
        )

        # Update history
        if hr_bpm is not None:
            self._hr_history.append((timestamp, hr_bpm))
        self._latest_estimate = estimate
        self._latest_timestamp = timestamp

        # Also estimate respiration from the same BVP window
        self._resp_estimator.process_bvp_window(
            bvp_window,
            blink_suppression=0.0,
            motion_proxy_signal=motion_resp_signal,
        )

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
                hrv_sdnn=None,
                hrv_pnn50=None,
                hrv_sd1=None,
                hrv_sd2=None,
                hrv_lf_hf_ratio=None,
                hrv_sample_entropy=None,
                physio_sqi=None,
                physio_sqi_components={},
                hr_delta_5s=None,
                valid=False,
            )

        hr_delta = self.compute_hr_delta(timestamp)

        resp_rate = None
        resp_est = self._resp_estimator.latest_estimate
        if resp_est is not None:
            resp_rate = resp_est.resp_rate_bpm

        nsqi = est.sqi_components.get("nsqi", 0.0)
        snr_db = est.sqi_components.get("snr_db", -99.0)
        sqi_gate_ok = (
            nsqi >= self._nsqi_threshold
            and snr_db >= self._min_cardiac_snr_db
            and est.physio_sqi >= 0.3
        )
        hrv_ready = len(self._get_rolling_ibi(timestamp)) >= self._hrv_min_valid_ibi

        pulse_bpm = est.hr_bpm if sqi_gate_ok else None
        hrv_rmssd = est.rmssd_ms if (sqi_gate_ok and hrv_ready) else None

        return PhysioFeatures(
            pulse_bpm=pulse_bpm,
            pulse_quality=est.physio_sqi,
            pulse_variability_proxy=hrv_rmssd,
            hrv_sdnn=est.sdnn_ms if (sqi_gate_ok and hrv_ready) else None,
            hrv_pnn50=est.pnn50 if (sqi_gate_ok and hrv_ready) else None,
            hrv_sd1=est.sd1_ms if (sqi_gate_ok and hrv_ready) else None,
            hrv_sd2=est.sd2_ms if (sqi_gate_ok and hrv_ready) else None,
            hrv_lf_hf_ratio=est.lf_hf_ratio if (sqi_gate_ok and hrv_ready) else None,
            hrv_sample_entropy=est.sample_entropy if (sqi_gate_ok and hrv_ready) else None,
            physio_sqi=est.physio_sqi,
            physio_sqi_components=dict(est.sqi_components),
            hr_delta_5s=hr_delta if sqi_gate_ok else None,
            respiration_rate_bpm=resp_rate,
            valid=sqi_gate_ok,
        )

    @property
    def resp_estimator(self) -> RespirationEstimator:
        """Access the respiration sub-estimator."""
        return self._resp_estimator

    def reset(self) -> None:
        """Reset all state."""
        self._hr_history.clear()
        self._ibi_history.clear()
        self._latest_estimate = None
        self._latest_timestamp = 0.0
        self._resp_estimator.reset()

    def _update_ibi_history(self, ibi_ms: NDArray[np.float64], timestamp: float) -> None:
        if ibi_ms.size == 0:
            return
        # Approximate each IBI timestamp relative to the current window endpoint.
        running = float(timestamp)
        for value in reversed(ibi_ms.tolist()):
            self._ibi_history.appendleft((running, float(value)))
            running -= max(0.2, float(value) / 1000.0)
        self._prune_ibi_history(timestamp)

    def _prune_ibi_history(self, now: float) -> None:
        cutoff = now - self._hrv_min_window_seconds
        while self._ibi_history and self._ibi_history[0][0] < cutoff:
            self._ibi_history.popleft()

    def _get_rolling_ibi(self, now: float) -> NDArray[np.float64]:
        self._prune_ibi_history(now)
        if not self._ibi_history:
            return np.array([], dtype=np.float64)
        return np.array([ibi for _, ibi in self._ibi_history], dtype=np.float64)
