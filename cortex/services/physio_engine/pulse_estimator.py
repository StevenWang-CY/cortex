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


@dataclass(frozen=True)
class StabilizedPulse:
    """Output of :class:`PulseStabilizer` for one window."""

    valid: bool
    bpm: float | None
    held: bool = False  # True when the value is a hold-through-dropout carry


class PulseStabilizer:
    """Temporal stabilizer for the per-window pulse validity + BPM (B2).

    The raw SQI gate (``nsqi & snr & physio_sqi``) is *stateless*, so a
    single marginal window — a blink, micro-motion, a lighting flicker —
    flips the reading from a live BPM to "acquiring" and back at the ~1 Hz
    stride rate. Users sitting still see a constant flicker. This wraps the
    raw per-window decision with three standard, literature-grounded
    post-processing steps:

    * **Schmitt-trigger lock** — *enter* the locked state only after
      ``enter_windows`` consecutive raw-valid windows; *leave* only when
      signal quality collapses below a **release** floor (``snr_release_db``
      / ``sqi_release``) that sits well under the enter thresholds. Marginal
      dips inside the hysteresis band do not unlock.
    * **Last-valid hold** — while locked, a raw-invalid window keeps emitting
      the last good (smoothed) BPM for up to ``grace_seconds`` before the
      reading is honestly released. This is what removes the flicker.
    * **BPM smoothing + slew limit** — report a trailing **median** of recent
      valid BPMs (pyVHR-style window statistics) and reject window-to-window
      jumps above ``max_slew_bpm_per_s`` (physiologically implausible),
      killing single-window spectral outliers.

    Refs: Wang et al. 2017 (POS); de Haan & Jeanne 2013 (CHROM); pyVHR
    (PeerJ CS 2022) post-processing; PMC13000236 "Adaptive physiology-
    informed correction" (temporal consistency / slew limiting).

    Pure and deterministic — fully unit-testable in isolation.
    """

    def __init__(
        self,
        *,
        enter_windows: int = 1,
        grace_seconds: float = 4.0,
        snr_release_db: float = 0.0,
        sqi_release: float = 0.20,
        smoothing_seconds: float = 6.0,
        max_slew_bpm_per_s: float = 12.0,
        bpm_floor: float = 30.0,
        bpm_ceil: float = 220.0,
    ) -> None:
        self._enter_windows = max(1, int(enter_windows))
        self._grace_seconds = max(0.0, float(grace_seconds))
        self._snr_release_db = float(snr_release_db)
        self._sqi_release = float(sqi_release)
        self._smoothing_seconds = max(0.0, float(smoothing_seconds))
        self._max_slew = max(0.0, float(max_slew_bpm_per_s))
        self._bpm_floor = float(bpm_floor)
        self._bpm_ceil = float(bpm_ceil)
        self.reset()

    def reset(self) -> None:
        self._locked = False
        self._consecutive_valid = 0
        self._last_valid_ts: float | None = None
        self._last_reported_bpm: float | None = None
        self._prev_slew_bpm: float | None = None
        self._prev_input_ts: float | None = None
        # (timestamp, slew-limited bpm) inputs feeding the trailing median.
        self._bpm_hist: deque[tuple[float, float]] = deque()

    # -- internals ---------------------------------------------------------
    def _apply_slew(self, timestamp: float, bpm: float) -> float:
        """Clamp ``bpm`` to within the physiological slew of the prior input."""
        if (
            self._max_slew <= 0.0
            or self._prev_slew_bpm is None
            or self._prev_input_ts is None
        ):
            return bpm
        dt = max(1e-3, timestamp - self._prev_input_ts)
        max_delta = self._max_slew * dt
        delta = bpm - self._prev_slew_bpm
        if abs(delta) > max_delta:
            return self._prev_slew_bpm + float(np.sign(delta)) * max_delta
        return bpm

    def _trim(self, now: float) -> None:
        cutoff = now - self._smoothing_seconds
        while self._bpm_hist and self._bpm_hist[0][0] < cutoff:
            self._bpm_hist.popleft()

    def _smoothed(self) -> float | None:
        if not self._bpm_hist:
            return None
        vals = [b for _, b in self._bpm_hist]
        return float(np.clip(np.median(vals), self._bpm_floor, self._bpm_ceil))

    # -- public ------------------------------------------------------------
    def update(
        self,
        *,
        timestamp: float,
        raw_valid: bool,
        bpm: float | None,
        snr_db: float | None = None,
        sqi: float | None = None,
    ) -> StabilizedPulse:
        """Fold one window's raw decision into the stabilized output."""
        if raw_valid and bpm is not None:
            self._consecutive_valid += 1
            self._last_valid_ts = timestamp
            bpm_in = self._apply_slew(timestamp, float(bpm))
            self._prev_slew_bpm = bpm_in
            self._prev_input_ts = timestamp
            self._bpm_hist.append((timestamp, bpm_in))
            self._trim(timestamp)
            if not self._locked and self._consecutive_valid >= self._enter_windows:
                self._locked = True
            if self._locked:
                reported = self._smoothed()
                self._last_reported_bpm = reported
                return StabilizedPulse(valid=True, bpm=reported, held=False)
            # Valid window but not yet locked → still acquiring.
            return StabilizedPulse(valid=False, bpm=None, held=False)

        # Raw window failed the gate (or no estimate at all).
        self._consecutive_valid = 0
        if self._locked and self._last_valid_ts is not None:
            within_grace = (timestamp - self._last_valid_ts) <= self._grace_seconds
            # Treat a missing snr/sqi (e.g. face briefly lost → no estimate)
            # as "not collapsed": rely on the grace timer to bound the hold.
            collapsed = (
                (snr_db is not None and snr_db < self._snr_release_db)
                or (sqi is not None and sqi < self._sqi_release)
            )
            if within_grace and not collapsed:
                self._trim(timestamp)
                held = self._last_reported_bpm
                if held is None:
                    held = self._smoothed()
                if held is not None:
                    return StabilizedPulse(valid=True, bpm=held, held=True)
        # Release: genuine signal loss.
        self._locked = False
        self._bpm_hist.clear()
        self._last_reported_bpm = None
        self._prev_slew_bpm = None
        self._prev_input_ts = None
        return StabilizedPulse(valid=False, bpm=None, held=False)


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
        stabilizer: PulseStabilizer | None = None,
    ) -> None:
        self._fs = fs
        self._low_hz = low_hz
        self._high_hz = high_hz
        self._filter_order = filter_order
        self._nsqi_threshold = nsqi_threshold
        self._min_cardiac_snr_db = min_cardiac_snr_db
        self._hrv_min_window_seconds = hrv_min_window_seconds
        self._hrv_min_valid_ibi = hrv_min_valid_ibi
        # B2: temporal stabilizer (hysteresis hold + BPM smoothing). When
        # None the estimator behaves exactly as before (stateless per-window
        # gate) — preserved so existing callers / tests are unaffected.
        self._stabilizer = stabilizer

        # Rolling HR history for delta computation
        # Store (timestamp, hr_bpm) pairs
        self._hr_history: deque[tuple[float, float]] = deque(
            maxlen=int(hr_history_seconds * 2)  # ~2 entries per second
        )

        # Latest estimates
        self._latest_estimate: PulseEstimate | None = None
        self._latest_timestamp: float = 0.0
        self._ibi_history: deque[tuple[float, float]] = deque(maxlen=2000)

        # Rolling head vertical-position history (normalized 0-1 image
        # coordinate) used to derive a motion-based respiratory proxy that
        # is fused with the BVP-derived respiration estimate. Sized to one
        # ~10 s window at the configured frame rate so the breathing band
        # (0.15-0.4 Hz) is resolvable.
        self._head_vertical_window_len = max(4, int(round(fs * 10.0)))
        self._head_vertical_history: deque[float] = deque(
            maxlen=self._head_vertical_window_len
        )

        # Respiration estimator (runs alongside cardiac estimation)
        self._resp_estimator = RespirationEstimator(fs=fs)

    @property
    def latest_estimate(self) -> PulseEstimate | None:
        """Get the most recent pulse estimate."""
        return self._latest_estimate

    def push_head_vertical_sample(self, y_normalized: float) -> None:
        """Record one frame's head vertical position for the motion proxy.

        ``y_normalized`` is the head/nose vertical position normalized to
        the [0, 1] image height (0 = top of frame). Respiratory motion
        modulates this slowly (chest/head bob), so a rolling buffer of these
        samples gives an independent respiratory signal that is fused with
        the BVP-derived estimate inside :meth:`process_window`. Callers that
        do not feed this (or supply an explicit ``motion_resp_signal``)
        simply fall back to BVP-only respiration.
        """
        self._head_vertical_history.append(float(y_normalized))

    def _motion_resp_signal(self) -> NDArray[np.float64] | None:
        """Build a motion-derived respiratory proxy from head vertical history.

        Returns a detrended 1-D signal long enough for the respiratory
        bandpass filter, or None when too few samples have been collected.
        """
        if len(self._head_vertical_history) < self._head_vertical_window_len:
            return None
        samples = np.asarray(self._head_vertical_history, dtype=np.float64)
        if samples.size < 4 or float(np.std(samples)) < 1e-9:
            return None
        # Remove the DC component; the respiratory bandpass downstream
        # isolates the breathing band from this displacement series.
        return samples - float(np.mean(samples))

    def process_window(
        self,
        bvp_window: NDArray[np.float64],
        timestamp: float = 0.0,
        *,
        fs: float | None = None,
        head_jitter_deg: float = 0.0,
        face_presence_ratio: float = 1.0,
        blink_suppression: float = 0.0,
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
            timestamp: Timestamp of the window center. Also threaded into
                the respiration estimator so the screen-apnea sustain
                timer uses the frame clock, not ``time.monotonic()``.
            head_jitter_deg: Inter-frame head jitter in degrees (motion penalty).
            face_presence_ratio: Fraction of the window with the face present.
            blink_suppression: Latest blink-suppression score (0-1). High
                values indicate visual fixation; forwarded to the
                respiration estimator to gate screen-apnea detection (C5).
            motion_resp_signal: Optional motion-derived respiratory proxy
                (e.g. head vertical displacement), fused with the BVP-derived
                respiration estimate when available.

        Returns:
            PulseEstimate with HR, HRV, and quality metrics.
        """
        # B1 (sampling-rate correctness): adopt the per-window effective fps
        # derived by the daemon from real frame timestamps. Welch HR =
        # dominant_freq × 60, so an honest fs is what makes the BPM correct;
        # trusting a hardcoded 30 fps on a camera running ~24-28 fps inflates
        # every reading. Every fs-dependent call below reads ``self._fs``, so
        # updating it once here (plus the respiration sub-estimator) threads
        # the corrected rate through the whole window. Ignored when the
        # caller passes nothing (tests / legacy callers keep the default).
        if fs is not None and fs > 0:
            self._fs = float(fs)
            self._resp_estimator.set_fs(float(fs))

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

        # Also estimate respiration from the same BVP window. C5: forward
        # the caller-supplied blink_suppression (1-frame lag is acceptable;
        # the daemon caches the previous frame's score) and the frame
        # timestamp so the apnea sustain timer is driven by the frame clock
        # rather than wall/monotonic time.
        #
        # Motion fusion: an explicit motion_resp_signal wins; otherwise the
        # estimator derives one from the head vertical-position history fed
        # via push_head_vertical_sample(). When neither is available the
        # respiration estimator runs BVP-only.
        motion_proxy = (
            motion_resp_signal
            if motion_resp_signal is not None
            else self._motion_resp_signal()
        )
        self._resp_estimator.process_bvp_window(
            bvp_window,
            blink_suppression=blink_suppression,
            motion_proxy_signal=motion_proxy,
            timestamp=timestamp,
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
            # No usable estimate this window. The stabilizer (B2) may still
            # HOLD a recent valid BPM through a brief dropout — one bad
            # window or a momentary face loss — so the reading does not
            # flicker. With no fresh estimate there is no snr/sqi to pass,
            # so the hold is bounded purely by the grace timer.
            if self._stabilizer is not None:
                stab = self._stabilizer.update(
                    timestamp=timestamp,
                    raw_valid=False,
                    bpm=None,
                    snr_db=None,
                    sqi=None,
                )
                if stab.valid and stab.bpm is not None:
                    return PhysioFeatures(
                        pulse_bpm=stab.bpm,
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
                        respiration_rate_bpm=None,
                        valid=True,
                    )
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
                respiration_rate_bpm=None,
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

        # B2: fold the raw per-window gate through the temporal stabilizer.
        # ``eff_valid`` drives the reported validity (with hysteresis +
        # hold); ``eff_bpm`` is the smoothed/held BPM. ``fresh`` stays True
        # only when THIS window passed the raw gate, so derived data (HRV,
        # respiration, hr_delta) is emitted from genuinely fresh estimates
        # — never carried during a hold.
        if self._stabilizer is not None:
            stab = self._stabilizer.update(
                timestamp=timestamp,
                raw_valid=sqi_gate_ok,
                bpm=est.hr_bpm,
                snr_db=snr_db,
                sqi=est.physio_sqi,
            )
            eff_valid = stab.valid
            eff_bpm = stab.bpm
        else:
            eff_valid = sqi_gate_ok
            eff_bpm = est.hr_bpm if sqi_gate_ok else None

        fresh = sqi_gate_ok
        hrv_ready = len(self._get_rolling_ibi(timestamp)) >= self._hrv_min_valid_ibi
        fresh_hrv = fresh and hrv_ready

        return PhysioFeatures(
            pulse_bpm=eff_bpm if eff_valid else None,
            pulse_quality=est.physio_sqi,
            pulse_variability_proxy=est.rmssd_ms if (eff_valid and fresh_hrv) else None,
            hrv_sdnn=est.sdnn_ms if (eff_valid and fresh_hrv) else None,
            hrv_pnn50=est.pnn50 if (eff_valid and fresh_hrv) else None,
            hrv_sd1=est.sd1_ms if (eff_valid and fresh_hrv) else None,
            hrv_sd2=est.sd2_ms if (eff_valid and fresh_hrv) else None,
            hrv_lf_hf_ratio=est.lf_hf_ratio if (eff_valid and fresh_hrv) else None,
            hrv_sample_entropy=est.sample_entropy if (eff_valid and fresh_hrv) else None,
            physio_sqi=est.physio_sqi,
            physio_sqi_components=dict(est.sqi_components),
            hr_delta_5s=hr_delta if (eff_valid and fresh) else None,
            # P0-1: respiration_rate_bpm is a data field — it MUST be None
            # when valid is False or PhysioFeatures._enforce_invalid_nulls
            # raises ValueError, which the daemon swallows as a dropped
            # frame (losing physio + kinematics for that stride).
            respiration_rate_bpm=resp_rate if (eff_valid and fresh) else None,
            valid=eff_valid,
        )

    @property
    def resp_estimator(self) -> RespirationEstimator:
        """Access the respiration sub-estimator."""
        return self._resp_estimator

    def reset(self) -> None:
        """Reset all state."""
        self._hr_history.clear()
        self._ibi_history.clear()
        self._head_vertical_history.clear()
        self._latest_estimate = None
        self._latest_timestamp = 0.0
        self._resp_estimator.reset()
        if self._stabilizer is not None:
            self._stabilizer.reset()

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
