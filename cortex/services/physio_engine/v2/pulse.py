"""Measured-time pulse windows feeding an absolute beat ledger."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import UUID

import numpy as np
from numpy.typing import NDArray
from scipy.signal import find_peaks, welch

from cortex.libs.schemas.physiology import (
    BeatCandidate,
    BeatEvent,
    BeatStatus,
    EstimateUncertainty,
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


def _stable_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(payload).hexdigest()[:24]}"


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
) -> tuple[float | None, float, float]:
    """Return harmonic-aware HR, peak concentration and native bin width."""

    if len(signal) < max(8, int(fs * 4.0)):
        return None, 0.0, float("inf")
    nperseg = min(len(signal), max(8, int(round(fs * 8.0))))
    nfft = max(nperseg, 2 ** int(np.ceil(np.log2(nperseg * 4))))
    frequencies, power = welch(
        signal,
        fs=fs,
        nperseg=nperseg,
        noverlap=nperseg // 2,
        nfft=nfft,
    )
    mask = (frequencies >= low_hz) & (frequencies <= high_hz)
    band_f = frequencies[mask]
    band_p = power[mask]
    total = float(np.sum(band_p))
    if len(band_p) < 3 or total <= 1e-15:
        return None, 0.0, float("inf")
    peaks, _ = find_peaks(band_p)
    if len(peaks) == 0:
        peaks = np.asarray([int(np.argmax(band_p))], dtype=np.intp)

    strongest = float(np.max(band_p[peaks]))
    scored: list[tuple[float, float, int]] = []
    for index in peaks:
        frequency = float(band_f[index])
        relative_power = float(band_p[index] / max(strongest, 1e-15))
        score = float(np.log(max(float(band_p[index]), 1e-15)))
        if prior_bpm is not None:
            score -= abs(frequency * 60.0 - prior_bpm) / 18.0

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
    return frequency * 60.0, float(np.clip(concentration, 0.0, 1.0)), bin_width


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
        minimum_window_quality: float = 0.30,
        experimental_hrv_enabled: bool = False,
        hrv_min_window_seconds: float = 180.0,
        hrv_min_valid_ibi: int = 120,
    ) -> None:
        self._backend = backend
        self._low_hz = float(low_hz)
        self._high_hz = float(high_hz)
        self._filter_order = int(filter_order)
        self._min_hr_bpm = float(min_hr_bpm)
        self._max_hr_bpm = float(max_hr_bpm)
        self._max_head_jitter_deg = float(max_head_jitter_deg)
        self._minimum_window_quality = float(minimum_window_quality)
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
            "minimum_window_quality": self._minimum_window_quality,
            "experimental_hrv_enabled": self._experimental_hrv_enabled,
            "hrv_min_window_seconds": self._hrv_min_window_seconds,
            "hrv_min_valid_ibi": self._hrv_min_valid_ibi,
        }
        self._algorithm_identity = SignalAlgorithmIdentity(
            name=f"pulse-v2:{backend.identity.name}",
            version="pulse-v2/2.0.0",
            implementation_sha256=code_sha256(
                (
                    PulsePipelineV2.process_window,
                    PulsePipelineV2._beat_candidates,
                    _spectral_hr,
                    _quadratic_peak_offset,
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

    @property
    def backend(self) -> ResolvedBackend:
        return self._backend

    @property
    def beat_events(self) -> tuple[BeatEvent, ...]:
        return self._ledger.events()

    @property
    def intervals(self) -> tuple[InterBeatInterval, ...]:
        return self._ledger.intervals()

    def reset(self) -> None:
        self._ledger.reset()
        self._prior_hr_bpm = None

    def process_window(
        self,
        rgb_window: NDArray[np.float64],
        sample_times_mono_ns: NDArray[np.int64],
        *,
        sample_rate_hz: float,
        boot_id: UUID,
        observation_quality: float,
        head_jitter_deg: float = 0.0,
        face_presence_ratio: float = 1.0,
    ) -> PulseProcessingResult:
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
        motion_penalty = float(np.clip(head_jitter_deg / 15.0, 0.0, 1.0))
        physio_sqi, _components = compute_physio_sqi(
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
        if head_jitter_deg > self._max_head_jitter_deg:
            quality = 0.0
        hr_bpm, hr_confidence, bin_width_hz = _spectral_hr(
            filtered,
            fs=sample_rate_hz,
            low_hz=self._low_hz,
            high_hz=self._high_hz,
            prior_bpm=self._prior_hr_bpm,
        )
        if hr_bpm is None or not self._min_hr_bpm <= hr_bpm <= self._max_hr_bpm:
            hr = self._unavailable_hr(
                boot_id, start_ns, end_ns, quality, "no plausible cardiac spectral peak"
            )
        elif quality < self._minimum_window_quality:
            hr = self._unavailable_hr(
                boot_id, start_ns, end_ns, quality, "window quality below publication gate"
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
                uncertainty=EstimateUncertainty(
                    lower=max(self._min_hr_bpm, float(hr_bpm) - half_width),
                    upper=min(self._max_hr_bpm, float(hr_bpm) + half_width),
                    confidence_level=0.95,
                    method="spectral-resolution-and-window-quality-bound",
                ),
                window_start_mono_ns=start_ns,
                window_end_mono_ns=end_ns,
                boot_id=boot_id,
            )
            if quality >= 0.45:
                self._prior_hr_bpm = float(hr_bpm)

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
                    np.interp(
                        refined_index,
                        np.arange(len(times), dtype=np.float64),
                        times.astype(np.float64),
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
