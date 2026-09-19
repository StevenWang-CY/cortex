"""Measured-time pulse windows feeding an absolute beat ledger."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from uuid import UUID

import numpy as np
from numpy.typing import NDArray
from scipy.signal import find_peaks, welch

from cortex.libs.schemas.physiology import (
    BeatCandidate,
    BeatEvent,
    BeatStatus,
    EvidenceStatus,
    InterBeatInterval,
    PhysiologyMetric,
    PulseWindowSummary,
    SignalAlgorithmIdentity,
    SignalEstimate,
)
from cortex.libs.signal.filters import bandpass_filter
from cortex.libs.signal.peak_detection import compute_physio_sqi
from cortex.services.physio_engine.v2.backends import ResolvedBackend
from cortex.services.physio_engine.v2.beats import BeatLedger
from cortex.services.physio_engine.v2.hrv import build_hrv_estimates
from cortex.services.physio_engine.v2.provenance import (
    code_sha256,
    configuration_sha256,
)
from cortex.services.physio_engine.v2.uncertainty import heuristic_interval

# --- HR prior ageing (D13) -------------------------------------------------
# The spectral peak selector prefers peaks near the previously published HR
# (``|delta_bpm| / PRIOR_PENALTY_SCALE_BPM`` is subtracted from the log-power
# score).  A prior that never aged penalised genuine rate changes forever
# after a quality dropout.  The penalty is therefore multiplied by
# ``0.5 ** (age_s / PRIOR_HALF_LIFE_SECONDS)`` where ``age_s`` is the time
# between the end of the window that set the prior and the end of the
# current window, and the prior is discarded entirely once it is older than
# ``PRIOR_MAX_AGE_SECONDS``.  With a 1 s stride and continuous quality the
# prior is at most ~1 s old (weight 0.93); after a 10 s dropout it carries
# half its weight; after 30 s it is dropped.
PRIOR_PENALTY_SCALE_BPM = 18.0
PRIOR_HALF_LIFE_SECONDS = 10.0
PRIOR_MAX_AGE_SECONDS = 30.0

# --- Sub-harmonic audit (bradycardia doubling) -----------------------------
# The publication band starts at ``low_hz`` (0.7 Hz = 42 BPM by default), so
# a genuine cardiac fundamental below that edge is removed by the bandpass
# while its second harmonic survives inside the band.  The spectral selector
# already prefers a fundamental over its harmonic, but only when the
# sub-harmonic is itself inside the analysed band -- see the ``half >= low_hz``
# guard in :func:`_spectral_hr`.  Below ``2 * low_hz`` it never asks, because
# the evidence it would need no longer exists in the filtered waveform.
#
# The evidence does still exist *before* the bandpass.  Re-filtering the same
# backend waveform with a lower edge recovers the fundamental, and the ratio
# of its power to the published peak's power separates the two populations by
# roughly two orders of magnitude.  Measured on the packaged POS backend over
# 40 synthetic windows per case (10 s, 30 fps, harmonic-rich BVP, 1% noise):
#
#   true rate   published   power(f/2) / power(f)
#   32 BPM      64.1        3.29
#   35 BPM      70.0        4.15
#   40 BPM      79.9        5.46
#   42 BPM      83.8        5.08
#   >= 45 BPM   correct     0.002 - 0.018
#
# ``SUBHARMONIC_RATIO_THRESHOLD`` sits ~17x above the largest observed
# false-positive ratio and ~11x below the smallest true-positive one.
# Respiration cannot forge this evidence: it modulates ROI brightness in
# common mode across R/G/B, which is precisely the component the POS
# projection cancels.  Driving respiration at exactly f/2 with three times a
# realistic amplitude moved the ratio only from 0.017 to 0.018.
# The audit runs when the sub-harmonic falls below
# ``SUBHARMONIC_AUDIT_MARGIN * low_hz``, not merely below ``low_hz``. A
# 4th-order Butterworth is -3 dB at its corner and rolls off gradually, so a
# fundamental a little *above* the edge is still attenuated enough to lose to
# its own second harmonic: at exactly the corner (42 BPM with the default
# 0.7 Hz band) 17 of 40 windows published a doubled rate while the strict
# ``< low_hz`` trigger stayed silent. 1.30 covers the transition region.
# Widening it is close to free because the ratio separates the populations by
# two orders of magnitude -- genuine rates in the newly audited span
# (84-109 BPM published) measure 0.002-0.018 against a 0.30 threshold.
SUBHARMONIC_AUDIT_LOW_HZ = 0.40
SUBHARMONIC_AUDIT_MARGIN = 1.30
# Which harmonics a removed fundamental can surface as. A real BVP upstroke
# carries appreciable 2nd *and* 3rd harmonic energy, so a 42 BPM fundamental
# can appear at 84 or at 126 BPM; checking only the octave left the triple
# published (measured: true 42 BPM -> 124.8 BPM). The 4th harmonic is not
# audited -- its amplitude is negligible and f/4 falls below the audit band
# for every rate the pipeline will publish.
SUBHARMONIC_AUDIT_DIVISORS = (2, 3)
SUBHARMONIC_RATIO_THRESHOLD = 0.30
SUBHARMONIC_PEAK_TOLERANCE_HZ = 0.06


# --- Legacy motion proxy (deprecated) --------------------------------------
# ``head_jitter_deg`` was ``nose displacement_px * 45 / frame_width`` and was
# scored as ``1 - jitter / 15`` with a hard gate at 7.5 deg; at 640 px that
# gate needed a 107 px/frame displacement, so it never fired.  The scale is
# kept only so un-migrated callers keep their previous (ineffective)
# behaviour; new callers pass ``motion_face_widths_per_second``.
LEGACY_HEAD_JITTER_PENALTY_SCALE_DEG = 15.0


def _stable_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(payload).hexdigest()[:24]}"


def prior_weight(age_seconds: float | None) -> float:
    """Return the multiplicative weight of an HR prior of the given age.

    ``None`` (no prior) and ages beyond :data:`PRIOR_MAX_AGE_SECONDS` yield
    ``0.0``; a brand-new prior yields ``1.0``.
    """

    if age_seconds is None or not np.isfinite(age_seconds):
        return 0.0
    age = max(0.0, float(age_seconds))
    if age > PRIOR_MAX_AGE_SECONDS:
        return 0.0
    return float(0.5 ** (age / PRIOR_HALF_LIFE_SECONDS))


def motion_penalty_from_face_widths(
    motion_face_widths_per_second: float | None,
    *,
    max_motion_face_widths_per_second: float,
) -> float:
    """Map window-mean facial translation speed to a ``[0, 1]`` SQI penalty.

    Unit: nose-tip translation speed in *face widths per second*, averaged
    over the valid samples of the window (see
    ``PreparedObservationWindow.mean_motion_face_widths_per_second``).  It is
    independent of resolution and frame rate.  The penalty is linear and
    reaches ``1.0`` at ``max_motion_face_widths_per_second``, the same
    threshold that gates publication.  At 30 fps on a 160 px-wide face a
    2 px/frame nose displacement is 0.375 face widths/s, i.e. a penalty of
    0.5 against the 0.75 default; 4 px/frame exceeds the gate.
    """

    if motion_face_widths_per_second is None:
        return 0.0
    if max_motion_face_widths_per_second <= 0:
        raise ValueError("max_motion_face_widths_per_second must be positive")
    value = float(motion_face_widths_per_second)
    if not np.isfinite(value) or value <= 0.0:
        return 0.0
    return float(np.clip(value / max_motion_face_widths_per_second, 0.0, 1.0))


def subharmonic_power_ratio(
    waveform: NDArray[np.float64],
    *,
    fs: float,
    published_hz: float,
    high_hz: float,
    audit_low_hz: float = SUBHARMONIC_AUDIT_LOW_HZ,
    divisors: tuple[int, ...] = SUBHARMONIC_AUDIT_DIVISORS,
) -> float:
    """Strongest sub-harmonic power relative to the published peak's power.

    For each ``k`` in *divisors* the power at ``published_hz / k`` is compared
    with the power at ``published_hz``; the largest ratio is returned. A value
    at or above :data:`SUBHARMONIC_RATIO_THRESHOLD` means the published peak
    is very likely the ``k``-th harmonic of a fundamental the publication
    band cannot represent.

    The ratio is measured on *waveform* -- the backend output before the
    publication bandpass -- re-filtered with ``audit_low_hz`` as the lower
    edge, so a fundamental the publication band removed is still present.
    Returns ``0.0`` when the window is too short to analyse or no sub-harmonic
    falls inside the audit band, i.e. when the question cannot be answered
    rather than when the answer is negative.
    """

    if published_hz >= high_hz or published_hz <= 0.0:
        return 0.0
    count = len(waveform)
    if count < max(8, int(fs * 4.0)):
        return 0.0
    targets = [
        published_hz / float(k)
        for k in divisors
        if published_hz / float(k) > audit_low_hz
    ]
    if not targets:
        return 0.0
    audited = bandpass_filter(
        waveform, low_hz=audit_low_hz, high_hz=high_hz, fs=fs, order=4
    )
    nfft = max(count, 2 ** int(np.ceil(np.log2(count * 4))))
    frequencies, power = welch(
        audited, fs=fs, window="hann", nperseg=count, noverlap=0,
        nfft=nfft, detrend="constant",
    )

    def _peak_near(target: float) -> float:
        selected = np.abs(frequencies - target) <= SUBHARMONIC_PEAK_TOLERANCE_HZ
        return float(np.max(power[selected])) if bool(selected.any()) else 0.0

    at_published = _peak_near(published_hz)
    if at_published <= 1e-18:
        return 0.0
    return max(float(_peak_near(target) / at_published) for target in targets)


@dataclass(frozen=True)
class SpectralPeak:
    """Harmonic-aware spectral HR selection over one whole window."""

    hr_bpm: float | None
    concentration: float
    bin_width_hz: float
    analysed_seconds: float
    native_resolution_hz: float


@dataclass(frozen=True)
class PulseProcessingResult:
    """Process-local waveform plus serializable evidence outputs."""

    waveform: NDArray[np.float64]
    filtered_waveform: NDArray[np.float64]
    summary: PulseWindowSummary
    candidates: tuple[BeatCandidate, ...]
    beat_events: tuple[BeatEvent, ...]
    intervals: tuple[InterBeatInterval, ...]
    hrv_estimates: dict[PhysiologyMetric, SignalEstimate]
    spectral_analysed_seconds: float = 0.0
    spectral_native_resolution_hz: float = float("inf")
    motion_penalty: float = 0.0
    prior_weight: float = 0.0
    # Gate evidence. ``sqi_components`` are the raw, un-normalised terms
    # behind ``summary.quality``; ``signal_present`` records whether this
    # window carried a cardiac signal at all, independently of how clean
    # the acquisition was. Process-local so the schema surface is unchanged.
    sqi_components: dict[str, float] = field(default_factory=dict)
    signal_present: bool = False
    subharmonic_ratio: float = 0.0


def _quadratic_peak_offset(signal: NDArray[np.float64], index: int) -> float:
    if index <= 0 or index >= len(signal) - 1:
        return 0.0
    left, center, right = signal[index - 1 : index + 2]
    denominator = left - 2.0 * center + right
    if abs(denominator) <= 1e-12:
        return 0.0
    return float(np.clip(0.5 * (left - right) / denominator, -0.5, 0.5))


def _spectral_hr(
    signal: NDArray[np.float64],
    *,
    fs: float,
    low_hz: float,
    high_hz: float,
    prior_bpm: float | None,
    prior_age_seconds: float | None = None,
) -> SpectralPeak:
    """Return harmonic-aware HR, peak concentration and spectral geometry.

    The whole window is analysed as one Hann-windowed periodogram
    (``nperseg == len(signal)``) with 4x zero padding for peak interpolation.
    The previous 8 s Welch segmentation silently dropped the tail of every
    10 s window (a single 240-sample segment at 30 fps), so the last 2 s never
    entered the estimate.  ``analysed_seconds`` reports the span that
    actually contributed.
    """

    count = len(signal)
    if count < max(8, int(fs * 4.0)):
        return SpectralPeak(None, 0.0, float("inf"), count / fs if fs > 0 else 0.0, float("inf"))
    nperseg = count
    nfft = max(nperseg, 2 ** int(np.ceil(np.log2(nperseg * 4))))
    frequencies, power = welch(
        signal,
        fs=fs,
        window="hann",
        nperseg=nperseg,
        noverlap=0,
        nfft=nfft,
        detrend="constant",
    )
    analysed_seconds = nperseg / fs
    native_resolution_hz = fs / nperseg
    mask = (frequencies >= low_hz) & (frequencies <= high_hz)
    band_f = frequencies[mask]
    band_p = power[mask]
    total = float(np.sum(band_p))
    if len(band_p) < 3 or total <= 1e-15:
        return SpectralPeak(None, 0.0, float("inf"), analysed_seconds, native_resolution_hz)
    peaks, _ = find_peaks(band_p)
    if len(peaks) == 0:
        peaks = np.asarray([int(np.argmax(band_p))], dtype=np.intp)

    weight = prior_weight(prior_age_seconds) if prior_bpm is not None else 0.0
    strongest = float(np.max(band_p[peaks]))
    scored: list[tuple[float, float, int]] = []
    for index in peaks:
        frequency = float(band_f[index])
        relative_power = float(band_p[index] / max(strongest, 1e-15))
        score = float(np.log(max(float(band_p[index]), 1e-15)))
        if prior_bpm is not None and weight > 0.0:
            score -= weight * abs(frequency * 60.0 - prior_bpm) / PRIOR_PENALTY_SCALE_BPM

        # A strong sub-harmonic is evidence that this peak is the second
        # harmonic, common with pulse waveforms that have a sharp systolic
        # shape. Prefer the plausible fundamental in that case.
        half = frequency / 2.0
        if half >= low_hz:
            half_index = int(np.argmin(np.abs(band_f - half)))
            half_power = float(band_p[half_index])
            if half_power >= 0.20 * float(band_p[index]):
                score -= 1.25
        scored.append((score, relative_power, int(index)))

    _, _, best_index = max(scored, key=lambda item: (item[0], item[1], -item[2]))
    if 0 < best_index < len(band_p) - 1:
        offset = _quadratic_peak_offset(band_p, best_index)
    else:
        offset = 0.0
    bin_width = float(frequencies[1] - frequencies[0])
    frequency = float(band_f[best_index] + offset * bin_width)
    neighbourhood = np.abs(band_f - frequency) <= max(0.10, 2.0 * bin_width)
    concentration = float(np.sum(band_p[neighbourhood]) / total)
    return SpectralPeak(
        hr_bpm=frequency * 60.0,
        concentration=float(np.clip(concentration, 0.0, 1.0)),
        bin_width_hz=bin_width,
        analysed_seconds=analysed_seconds,
        native_resolution_hz=native_resolution_hz,
    )


class PulsePipelineV2:
    """One fixed backend, one monotonic beat ledger, explicit readiness."""

    def __init__(
        self,
        backend: ResolvedBackend,
        *,
        low_hz: float = 0.7,
        high_hz: float = 3.5,
        filter_order: int = 4,
        min_hr_bpm: float = 30.0,
        max_hr_bpm: float = 210.0,
        max_head_jitter_deg: float = 7.5,
        max_motion_face_widths_per_second: float = 0.75,
        minimum_window_quality: float = 0.30,
        nsqi_threshold: float = 0.293,
        min_cardiac_snr_db: float = 2.0,
        experimental_hrv_enabled: bool = False,
        hrv_min_window_seconds: float = 180.0,
        hrv_min_valid_ibi: int = 120,
    ) -> None:
        if max_motion_face_widths_per_second <= 0:
            raise ValueError("max_motion_face_widths_per_second must be positive")
        self._backend = backend
        self._low_hz = float(low_hz)
        self._high_hz = float(high_hz)
        self._filter_order = int(filter_order)
        self._min_hr_bpm = float(min_hr_bpm)
        self._max_hr_bpm = float(max_hr_bpm)
        self._max_head_jitter_deg = float(max_head_jitter_deg)
        self._max_motion_fw_s = float(max_motion_face_widths_per_second)
        self._minimum_window_quality = float(minimum_window_quality)
        self._nsqi_threshold = float(nsqi_threshold)
        self._min_cardiac_snr_db = float(min_cardiac_snr_db)
        self._experimental_hrv_enabled = bool(experimental_hrv_enabled)
        self._hrv_min_window_seconds = float(hrv_min_window_seconds)
        self._hrv_min_valid_ibi = int(hrv_min_valid_ibi)
        parameters: dict[str, str | int | float | bool] = {
            "bvp_backend": backend.identity.name,
            "bvp_backend_version": backend.identity.version,
            "bvp_backend_sha256": backend.identity.implementation_sha256,
            "bandpass_low_hz": self._low_hz,
            "bandpass_high_hz": self._high_hz,
            "bandpass_order": self._filter_order,
            "min_hr_bpm": self._min_hr_bpm,
            "max_hr_bpm": self._max_hr_bpm,
            "max_head_jitter_deg": self._max_head_jitter_deg,
            "max_motion_face_widths_per_second": self._max_motion_fw_s,
            "minimum_window_quality": self._minimum_window_quality,
            "nsqi_threshold": self._nsqi_threshold,
            "min_cardiac_snr_db": self._min_cardiac_snr_db,
            "subharmonic_audit_low_hz": SUBHARMONIC_AUDIT_LOW_HZ,
            "subharmonic_audit_margin": SUBHARMONIC_AUDIT_MARGIN,
            "subharmonic_audit_divisors": ",".join(
                str(k) for k in SUBHARMONIC_AUDIT_DIVISORS
            ),
            "subharmonic_ratio_threshold": SUBHARMONIC_RATIO_THRESHOLD,
            "prior_half_life_seconds": PRIOR_HALF_LIFE_SECONDS,
            "prior_max_age_seconds": PRIOR_MAX_AGE_SECONDS,
            "experimental_hrv_enabled": self._experimental_hrv_enabled,
            "hrv_min_window_seconds": self._hrv_min_window_seconds,
            "hrv_min_valid_ibi": self._hrv_min_valid_ibi,
        }
        self._algorithm_identity = SignalAlgorithmIdentity(
            name=f"pulse-v2:{backend.identity.name}",
            version="pulse-v2/2.2.0",
            implementation_sha256=code_sha256(
                (
                    PulsePipelineV2.process_window,
                    PulsePipelineV2._beat_candidates,
                    _spectral_hr,
                    subharmonic_power_ratio,
                    _quadratic_peak_offset,
                    prior_weight,
                    motion_penalty_from_face_widths,
                    BeatLedger.ingest,
                    BeatLedger._derive_intervals,
                ),
                dependency_sha256=(backend.identity.implementation_sha256,),
            ),
            asset_sha256=backend.identity.asset_sha256,
            configuration_sha256=configuration_sha256(parameters),
            parameters=parameters,
            selection_mode=backend.identity.selection_mode,
        )
        self._ledger = BeatLedger(
            min_hr_bpm=min_hr_bpm,
            max_hr_bpm=max_hr_bpm,
        )
        self._prior_hr_bpm: float | None = None
        self._prior_set_at_mono_ns: int | None = None

    @property
    def backend(self) -> ResolvedBackend:
        return self._backend

    @property
    def beat_events(self) -> tuple[BeatEvent, ...]:
        return self._ledger.events()

    @property
    def intervals(self) -> tuple[InterBeatInterval, ...]:
        return self._ledger.intervals()

    @property
    def prior_hr_bpm(self) -> float | None:
        return self._prior_hr_bpm

    def prior_age_seconds(self, at_mono_ns: int) -> float | None:
        """Age of the current HR prior relative to a window end time."""

        if self._prior_hr_bpm is None or self._prior_set_at_mono_ns is None:
            return None
        return max(0.0, (int(at_mono_ns) - self._prior_set_at_mono_ns) / 1_000_000_000.0)

    @property
    def algorithm_identity(self) -> SignalAlgorithmIdentity:
        """Provenance for every estimate this pipeline publishes."""
        return self._algorithm_identity

    def reset(self) -> None:
        self._ledger.reset()
        self._prior_hr_bpm = None
        self._prior_set_at_mono_ns = None

    def process_window(
        self,
        rgb_window: NDArray[np.float64],
        sample_times_mono_ns: NDArray[np.int64],
        *,
        sample_rate_hz: float,
        boot_id: UUID,
        observation_quality: float,
        motion_face_widths_per_second: float | None = None,
        face_presence_ratio: float = 1.0,
        head_jitter_deg: float | None = None,
    ) -> PulseProcessingResult:
        """Process one uniformly resampled RGB window.

        ``motion_face_widths_per_second`` is the window-mean nose-tip
        translation speed in face widths per second and is the authoritative
        motion evidence.  ``head_jitter_deg`` is the deprecated legacy proxy;
        it is consulted only when the face-width evidence is absent and then
        keeps its historical (practically unreachable) semantics.
        """

        rgb = np.asarray(rgb_window, dtype=np.float64)
        times = np.asarray(sample_times_mono_ns, dtype=np.int64)
        if rgb.ndim != 2 or rgb.shape[1] != 3 or len(rgb) != len(times):
            raise ValueError("RGB samples and monotonic time grid must align")
        if len(times) < 2 or bool((np.diff(times) <= 0).any()):
            raise ValueError("pulse sample times must be strictly increasing")
        if not bool(np.isfinite(rgb).all()):
            raise ValueError("pulse RGB window must be finite")
        start_ns = int(times[0])
        end_ns = int(times[-1])
        window_id = _stable_id(
            "pulse_window",
            boot_id,
            start_ns,
            end_ns,
            self._algorithm_identity.implementation_sha256,
            self._algorithm_identity.configuration_sha256,
        )

        waveform = self._backend.extract(rgb, fs=sample_rate_hz)
        filtered = bandpass_filter(
            waveform,
            low_hz=self._low_hz,
            high_hz=self._high_hz,
            fs=sample_rate_hz,
            order=self._filter_order,
        )
        motion_gate_exceeded = False
        if motion_face_widths_per_second is not None:
            motion_penalty = motion_penalty_from_face_widths(
                motion_face_widths_per_second,
                max_motion_face_widths_per_second=self._max_motion_fw_s,
            )
            motion_gate_exceeded = (
                float(motion_face_widths_per_second) > self._max_motion_fw_s
            )
        elif head_jitter_deg is not None:
            motion_penalty = float(
                np.clip(head_jitter_deg / LEGACY_HEAD_JITTER_PENALTY_SCALE_DEG, 0.0, 1.0)
            )
            motion_gate_exceeded = head_jitter_deg > self._max_head_jitter_deg
        else:
            motion_penalty = 0.0
        physio_sqi, sqi_components = compute_physio_sqi(
            waveform,
            fs=sample_rate_hz,
            low_hz=self._low_hz,
            high_hz=self._high_hz,
            motion_penalty=motion_penalty,
            face_presence_ratio=face_presence_ratio,
        )
        # Acquisition and signal quality are conjunctive evidence. A clean
        # spectrum cannot erase a motion/coverage failure upstream.
        quality = float(
            np.clip(physio_sqi * observation_quality, 0.0, 1.0)
        )
        if motion_gate_exceeded:
            quality = 0.0
        prior_age = self.prior_age_seconds(end_ns)
        weight = prior_weight(prior_age) if self._prior_hr_bpm is not None else 0.0
        peak = _spectral_hr(
            filtered,
            fs=sample_rate_hz,
            low_hz=self._low_hz,
            high_hz=self._high_hz,
            prior_bpm=self._prior_hr_bpm,
            prior_age_seconds=prior_age,
        )
        hr_bpm = peak.hr_bpm
        hr_confidence = peak.concentration
        bin_width_hz = peak.bin_width_hz

        # Signal presence is tested on the raw, un-normalised spectrum terms,
        # NOT on ``quality``. ``compute_physio_sqi`` is an additive blend in
        # which the acquisition terms alone contribute 0.25 of a possible
        # 1.0 (``0.15 * motion_term + 0.10 * face_term``), so a still, fully
        # visible face scores above the 0.30 publication gate before any
        # cardiac evidence is considered at all. Measured over 200 windows
        # per condition on the packaged POS backend, signal-free input scored
        # 0.334-0.449 (white noise) and 0.443-0.684 (1/f and drift noise):
        # every single signal-free window cleared the gate, and the pipeline
        # published a fabricated rate for all of them.
        #
        # Normalising SNR to [0, 1] is what destroys the discrimination --
        # ``(snr_db + 10) / 20`` compresses the whole decision range into the
        # middle of the scale, where averaging dilutes it further. Thresholding
        # the raw decibel value keeps it: ``snr_db >= 2.0`` rejected 98.5-100%
        # of signal-free windows across all three noise models. ``nsqi`` adds
        # no discrimination against 1/f noise (it passes ~94% of it) but costs
        # no sensitivity either and rejects 100% of white noise, so it is kept
        # as a cheap guard on the degenerate flat-spectrum case.
        nsqi = float(sqi_components.get("nsqi", 0.0))
        snr_db = float(sqi_components.get("snr_db", -99.0))
        signal_present = (
            nsqi >= self._nsqi_threshold and snr_db >= self._min_cardiac_snr_db
        )

        # A peak whose own sub-harmonic sits below the publication band may be
        # the second harmonic of a bradycardic fundamental the bandpass
        # removed. ``_spectral_hr`` cannot tell -- its evidence was filtered
        # away -- so the question is re-asked against a wider view of the same
        # backend waveform. Only computed when it can change the answer.
        subharmonic_ratio = 0.0
        audit_below_bpm = self._low_hz * 60.0 * SUBHARMONIC_AUDIT_MARGIN
        if hr_bpm is not None and any(
            (hr_bpm / float(k)) < audit_below_bpm for k in SUBHARMONIC_AUDIT_DIVISORS
        ):
            subharmonic_ratio = subharmonic_power_ratio(
                waveform,
                fs=sample_rate_hz,
                published_hz=hr_bpm / 60.0,
                high_hz=self._high_hz,
            )

        if hr_bpm is None or not self._min_hr_bpm <= hr_bpm <= self._max_hr_bpm:
            hr = self._unavailable_hr(
                boot_id, start_ns, end_ns, quality, "no plausible cardiac spectral peak"
            )
        elif not signal_present:
            hr = self._unavailable_hr(
                boot_id,
                start_ns,
                end_ns,
                quality,
                "no cardiac signal above the noise floor",
            )
        elif quality < self._minimum_window_quality:
            hr = self._unavailable_hr(
                boot_id, start_ns, end_ns, quality, "window quality below publication gate"
            )
        elif subharmonic_ratio >= SUBHARMONIC_RATIO_THRESHOLD:
            # Publishing the peak would double a real bradycardic rate; the
            # fundamental itself is not measurable in the publication band,
            # so neither value can be stated.
            hr = self._unavailable_hr(
                boot_id,
                start_ns,
                end_ns,
                quality,
                "cardiac fundamental below the analysis band",
            )
        else:
            half_width = max(
                bin_width_hz * 30.0,
                1.5,
                5.0 * (1.0 - min(quality, hr_confidence)),
            )
            hr = SignalEstimate(
                metric=PhysiologyMetric.HEART_RATE,
                value=float(hr_bpm),
                unit="bpm",
                # Reference-sensor G5/WP-11 is not complete, so the new
                # estimate is intentionally not labelled supported.
                status=EvidenceStatus.EXPERIMENTAL,
                quality=quality,
                algorithm=self._algorithm_identity,
                uncertainty=heuristic_interval(
                    lower=max(self._min_hr_bpm, float(hr_bpm) - half_width),
                    upper=min(self._max_hr_bpm, float(hr_bpm) + half_width),
                    method="spectral-resolution-and-window-quality-bound",
                ),
                window_start_mono_ns=start_ns,
                window_end_mono_ns=end_ns,
                boot_id=boot_id,
            )
            if quality >= 0.45:
                self._prior_hr_bpm = float(hr_bpm)
                self._prior_set_at_mono_ns = end_ns

        candidates = self._beat_candidates(
            filtered,
            times,
            sample_rate_hz=sample_rate_hz,
            window_id=window_id,
            window_quality=quality,
        )
        boundary_margin_ns = int(0.75 * 1_000_000_000)
        events, intervals = self._ledger.ingest(
            candidates,
            window_id=window_id,
            window_start_mono_ns=start_ns,
            window_end_mono_ns=end_ns,
            boundary_margin_ns=boundary_margin_ns,
        )
        hrv = build_hrv_estimates(
            intervals,
            algorithm=self._algorithm_identity,
            boot_id=boot_id,
            enabled=self._experimental_hrv_enabled,
            rmssd_min_seconds=self._hrv_min_window_seconds,
            sdnn_min_seconds=max(300.0, self._hrv_min_window_seconds),
            min_valid_ibi=self._hrv_min_valid_ibi,
        )
        window_events = [item for item in events if window_id in item.source_window_ids]
        summary = PulseWindowSummary(
            window_id=window_id,
            boot_id=boot_id,
            window_start_mono_ns=start_ns,
            window_end_mono_ns=end_ns,
            sample_rate_hz=sample_rate_hz,
            sample_count=len(rgb),
            algorithm=self._algorithm_identity,
            quality=quality,
            hr=hr,
            candidate_count=len(candidates),
            accepted_beat_count=sum(
                item.status == BeatStatus.ACCEPTED.value for item in window_events
            ),
            provisional_beat_count=sum(
                item.status == BeatStatus.PROVISIONAL.value for item in window_events
            ),
            rejected_beat_count=sum(
                item.status == BeatStatus.REJECTED.value for item in window_events
            ),
        )
        return PulseProcessingResult(
            waveform=waveform,
            filtered_waveform=filtered,
            summary=summary,
            candidates=candidates,
            beat_events=events,
            intervals=intervals,
            hrv_estimates=hrv,
            spectral_analysed_seconds=peak.analysed_seconds,
            spectral_native_resolution_hz=peak.native_resolution_hz,
            motion_penalty=motion_penalty,
            prior_weight=weight,
            sqi_components=dict(sqi_components),
            signal_present=signal_present,
            subharmonic_ratio=subharmonic_ratio,
        )

    def _unavailable_hr(
        self,
        boot_id: UUID,
        start_ns: int,
        end_ns: int,
        quality: float,
        reason: str,
    ) -> SignalEstimate:
        return SignalEstimate(
            metric=PhysiologyMetric.HEART_RATE,
            value=None,
            unit="bpm",
            status=EvidenceStatus.REJECTED,
            quality=quality,
            algorithm=self._algorithm_identity,
            unavailable_reason=reason,
            window_start_mono_ns=start_ns,
            window_end_mono_ns=end_ns,
            boot_id=boot_id,
        )

    def _beat_candidates(
        self,
        signal: NDArray[np.float64],
        times: NDArray[np.int64],
        *,
        sample_rate_hz: float,
        window_id: str,
        window_quality: float,
    ) -> tuple[BeatCandidate, ...]:
        signal_range = float(np.ptp(signal))
        if signal_range <= 1e-12:
            return ()
        minimum_distance = max(
            1, int(np.floor(sample_rate_hz * 60.0 / self._max_hr_bpm))
        )
        indices, properties = find_peaks(
            signal,
            distance=minimum_distance,
            prominence=0.10 * signal_range,
        )
        prominences = np.asarray(properties.get("prominences", []), dtype=np.float64)
        max_prominence = float(np.max(prominences)) if len(prominences) else 1.0
        boundary_margin_ns = 750_000_000
        result: list[BeatCandidate] = []
        for position, index in enumerate(indices):
            refined_index = float(index) + _quadratic_peak_offset(signal, int(index))
            absolute_ns = int(
                round(
                    float(
                        np.interp(
                            refined_index,
                            np.arange(len(times), dtype=np.float64),
                            times.astype(np.float64),
                        )
                    )
                )
            )
            prominence = float(prominences[position]) if position < len(prominences) else 0.0
            normalized_prominence = prominence / max(max_prominence, 1e-12)
            candidate_quality = float(
                np.clip(window_quality * (0.40 + 0.60 * normalized_prominence), 0.0, 1.0)
            )
            near_boundary = (
                absolute_ns - int(times[0]) < boundary_margin_ns
                or int(times[-1]) - absolute_ns < boundary_margin_ns
            )
            result.append(
                BeatCandidate(
                    candidate_id=_stable_id("candidate", window_id, absolute_ns),
                    absolute_mono_ns=absolute_ns,
                    prominence=prominence,
                    quality=candidate_quality,
                    source_window_id=window_id,
                    near_window_boundary=near_boundary,
                )
            )
        return tuple(result)
