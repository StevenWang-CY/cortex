"""Long-window, multi-channel respiratory-rate estimation.

This module estimates a breathing-rate proxy. It does not diagnose apnea or
infer a medical condition. Cross-channel disagreement causes abstention.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import numpy as np
from numpy.typing import NDArray
from scipy.signal import detrend, hilbert, welch

from cortex.libs.schemas.physiology import (
    EstimateUncertainty,
    EvidenceStatus,
    PhysiologyMetric,
    SignalAlgorithmIdentity,
    SignalEstimate,
)
from cortex.libs.signal.filters import bandpass_filter
from cortex.services.physio_engine.v2.backends import ResolvedBackend
from cortex.services.physio_engine.v2.provenance import (
    code_sha256,
    configuration_sha256,
)


@dataclass(frozen=True)
class _ChannelEstimate:
    rate_bpm: float
    quality: float
    resolution_bpm: float


@dataclass(frozen=True)
class RespirationProcessingResult:
    """Per-channel evidence and the conservative fused publication result."""

    channels: dict[str, SignalEstimate]
    fused: SignalEstimate


def _spectral_channel(
    signal: NDArray[np.float64] | None,
    *,
    fs: float,
    low_hz: float,
    high_hz: float,
    min_window_seconds: float,
) -> _ChannelEstimate | None:
    if signal is None:
        return None
    values = np.asarray(signal, dtype=np.float64).reshape(-1)
    if (
        fs <= 0
        or len(values) < int(np.ceil(min_window_seconds * fs))
        or not bool(np.isfinite(values).all())
        or float(np.std(values)) <= 1e-10
    ):
        return None
    try:
        filtered = bandpass_filter(
            detrend(values, type="linear"),
            low_hz=low_hz,
            high_hz=high_hz,
            fs=fs,
            order=3,
        )
    except ValueError:
        return None
    nperseg = len(filtered)
    nfft = max(nperseg, 2 ** int(np.ceil(np.log2(nperseg * 4))))
    frequencies, power = welch(
        filtered,
        fs=fs,
        nperseg=nperseg,
        noverlap=0,
        nfft=nfft,
    )
    band = (frequencies >= low_hz) & (frequencies <= high_hz)
    band_f = frequencies[band]
    band_p = power[band]
    total = float(np.sum(band_p))
    if len(band_p) < 3 or total <= 1e-15:
        return None
    peak = int(np.argmax(band_p))
    frequency = float(band_f[peak])
    resolution_hz = float(frequencies[1] - frequencies[0])
    neighbourhood = np.abs(band_f - frequency) <= max(0.025, 2 * resolution_hz)
    concentration = float(np.sum(band_p[neighbourhood]) / total)
    observed_cycles = frequency * (len(values) / fs)
    cycle_factor = float(np.clip(observed_cycles / 5.0, 0.0, 1.0))
    quality = float(np.clip(concentration * cycle_factor, 0.0, 1.0))
    return _ChannelEstimate(
        rate_bpm=frequency * 60.0,
        quality=quality,
        resolution_bpm=resolution_hz * 60.0,
    )


def _color_envelope(
    waveform: NDArray[np.float64],
    *,
    fs: float,
    cardiac_low_hz: float,
    cardiac_high_hz: float,
) -> NDArray[np.float64] | None:
    try:
        cardiac = bandpass_filter(
            waveform,
            low_hz=cardiac_low_hz,
            high_hz=cardiac_high_hz,
            fs=fs,
            order=3,
        )
    except ValueError:
        return None
    envelope = np.abs(hilbert(cardiac))
    if not bool(np.isfinite(envelope).all()):
        return None
    return np.asarray(detrend(envelope, type="linear"), dtype=np.float64)


class RespirationFusionV2:
    """Fuse color-envelope and face-normalized vertical-motion channels."""

    def __init__(
        self,
        backend: ResolvedBackend,
        *,
        low_hz: float = 0.08,
        high_hz: float = 0.50,
        min_window_seconds: float = 30.0,
        minimum_channel_quality: float = 0.35,
        max_channel_disagreement_bpm: float = 3.0,
        experimental_publication_enabled: bool = False,
    ) -> None:
        if not 0 < low_hz < high_hz:
            raise ValueError("respiration band must be positive and ordered")
        if not 30.0 <= min_window_seconds <= 60.0:
            raise ValueError("respiration requires a 30-60 second window")
        self._backend = backend
        self._low_hz = float(low_hz)
        self._high_hz = float(high_hz)
        self._min_window_seconds = float(min_window_seconds)
        self._minimum_channel_quality = float(minimum_channel_quality)
        self._max_disagreement = float(max_channel_disagreement_bpm)
        self._publication_enabled = bool(experimental_publication_enabled)
        common_parameters: dict[str, str | int | float | bool] = {
            "low_hz": self._low_hz,
            "high_hz": self._high_hz,
            "min_window_seconds": self._min_window_seconds,
            "minimum_channel_quality": self._minimum_channel_quality,
            "max_channel_disagreement_bpm": self._max_disagreement,
            "experimental_publication_enabled": self._publication_enabled,
        }
        color_parameters = {
            **common_parameters,
            "bvp_backend": backend.identity.name,
            "bvp_backend_version": backend.identity.version,
            "bvp_backend_sha256": backend.identity.implementation_sha256,
        }
        self._identities = {
            "color_envelope": SignalAlgorithmIdentity(
                name=f"color-envelope-respiration:{backend.identity.name}",
                version="2.0.0",
                implementation_sha256=code_sha256(
                    (_color_envelope, _spectral_channel),
                    dependency_sha256=(backend.identity.implementation_sha256,),
                ),
                asset_sha256=backend.identity.asset_sha256,
                configuration_sha256=configuration_sha256(color_parameters),
                parameters=color_parameters,
                selection_mode="fixed",
            ),
            "head_motion": SignalAlgorithmIdentity(
                name="face-normalized-head-motion-respiration",
                version="2.0.0",
                implementation_sha256=code_sha256((_spectral_channel,)),
                configuration_sha256=configuration_sha256(common_parameters),
                parameters=common_parameters,
                selection_mode="fixed",
            ),
            "fusion": SignalAlgorithmIdentity(
                name="agreement-gated-respiration-fusion",
                version="2.0.0",
                implementation_sha256=code_sha256(
                    (RespirationFusionV2.process_window, _spectral_channel),
                    dependency_sha256=(
                        code_sha256(
                            (_color_envelope, _spectral_channel),
                            dependency_sha256=(
                                backend.identity.implementation_sha256,
                            ),
                        ),
                        code_sha256((_spectral_channel,)),
                    ),
                ),
                configuration_sha256=configuration_sha256(common_parameters),
                parameters=common_parameters,
                selection_mode="fixed",
            ),
        }

    def process_window(
        self,
        rgb_window: NDArray[np.float64],
        sample_times_mono_ns: NDArray[np.int64],
        *,
        sample_rate_hz: float,
        boot_id: UUID,
        head_vertical_face_units: NDArray[np.float64] | None,
    ) -> RespirationProcessingResult:
        rgb = np.asarray(rgb_window, dtype=np.float64)
        times = np.asarray(sample_times_mono_ns, dtype=np.int64)
        if rgb.ndim != 2 or rgb.shape[1] != 3 or len(rgb) != len(times):
            raise ValueError("respiration RGB samples and time grid must align")
        if len(times) < 2 or bool((np.diff(times) <= 0).any()):
            raise ValueError("respiration sample times must be strictly increasing")
        start_ns, end_ns = int(times[0]), int(times[-1])
        duration_s = (end_ns - start_ns) / 1_000_000_000.0
        if duration_s < self._min_window_seconds:
            reason = f"requires at least {self._min_window_seconds:.0f} seconds"
            channels = {
                name: self._unavailable(name, reason, boot_id, start_ns, end_ns)
                for name in ("color_envelope", "head_motion")
            }
            return RespirationProcessingResult(
                channels=channels,
                fused=self._unavailable("fusion", reason, boot_id, start_ns, end_ns),
            )

        waveform = self._backend.extract(rgb, fs=sample_rate_hz)
        color_signal = _color_envelope(
            waveform,
            fs=sample_rate_hz,
            cardiac_low_hz=0.7,
            cardiac_high_hz=3.5,
        )
        raw_channels = {
            "color_envelope": _spectral_channel(
                color_signal,
                fs=sample_rate_hz,
                low_hz=self._low_hz,
                high_hz=self._high_hz,
                min_window_seconds=self._min_window_seconds,
            ),
            "head_motion": _spectral_channel(
                head_vertical_face_units,
                fs=sample_rate_hz,
                low_hz=self._low_hz,
                high_hz=self._high_hz,
                min_window_seconds=self._min_window_seconds,
            ),
        }
        channels = {
            name: self._channel_result(name, estimate, boot_id, start_ns, end_ns)
            for name, estimate in raw_channels.items()
        }
        usable = {
            name: estimate
            for name, estimate in raw_channels.items()
            if estimate is not None
            and estimate.quality >= self._minimum_channel_quality
        }
        if len(usable) < 2:
            fused = self._unavailable(
                "fusion",
                "requires two quality-qualified independent channels",
                boot_id,
                start_ns,
                end_ns,
            )
        else:
            rates = [item.rate_bpm for item in usable.values()]
            disagreement = max(rates) - min(rates)
            if disagreement > self._max_disagreement:
                fused = self._unavailable(
                    "fusion",
                    "independent respiration channels disagree",
                    boot_id,
                    start_ns,
                    end_ns,
                )
            elif not self._publication_enabled:
                fused = self._unavailable(
                    "fusion",
                    "disabled pending simultaneous reference-respiration validation",
                    boot_id,
                    start_ns,
                    end_ns,
                    quality=min(item.quality for item in usable.values()),
                )
            else:
                weights = np.asarray([item.quality for item in usable.values()])
                values = np.asarray(rates)
                rate = float(np.average(values, weights=weights))
                half_width = max(
                    1.5,
                    disagreement / 2.0,
                    max(item.resolution_bpm for item in usable.values()),
                )
                fused = SignalEstimate(
                    metric=PhysiologyMetric.RESPIRATION_RATE,
                    value=rate,
                    unit="breaths/min",
                    status=EvidenceStatus.EXPERIMENTAL,
                    quality=float(min(item.quality for item in usable.values())),
                    algorithm=self._identities["fusion"],
                    uncertainty=EstimateUncertainty(
                        lower=max(0.0, rate - half_width),
                        upper=rate + half_width,
                        confidence_level=0.95,
                        method="channel-agreement-and-spectral-resolution-bound",
                    ),
                    window_start_mono_ns=start_ns,
                    window_end_mono_ns=end_ns,
                    boot_id=boot_id,
                )
        return RespirationProcessingResult(channels=channels, fused=fused)

    def _channel_result(
        self,
        name: str,
        estimate: _ChannelEstimate | None,
        boot_id: UUID,
        start_ns: int,
        end_ns: int,
    ) -> SignalEstimate:
        if estimate is None:
            return self._unavailable(
                name, "channel has insufficient periodic evidence", boot_id, start_ns, end_ns
            )
        if estimate.quality < self._minimum_channel_quality:
            return self._unavailable(
                name,
                "channel quality below gate",
                boot_id,
                start_ns,
                end_ns,
                quality=estimate.quality,
            )
        half_width = max(1.5, estimate.resolution_bpm)
        return SignalEstimate(
            metric=PhysiologyMetric.RESPIRATION_RATE,
            value=estimate.rate_bpm,
            unit="breaths/min",
            status=EvidenceStatus.EXPERIMENTAL,
            quality=estimate.quality,
            algorithm=self._identities[name],
            uncertainty=EstimateUncertainty(
                lower=max(0.0, estimate.rate_bpm - half_width),
                upper=estimate.rate_bpm + half_width,
                confidence_level=0.95,
                method="spectral-resolution-bound",
            ),
            window_start_mono_ns=start_ns,
            window_end_mono_ns=end_ns,
            boot_id=boot_id,
        )

    def _unavailable(
        self,
        name: str,
        reason: str,
        boot_id: UUID,
        start_ns: int,
        end_ns: int,
        *,
        quality: float = 0.0,
    ) -> SignalEstimate:
        return SignalEstimate(
            metric=PhysiologyMetric.RESPIRATION_RATE,
            value=None,
            unit="breaths/min",
            status=EvidenceStatus.UNAVAILABLE,
            quality=quality,
            algorithm=self._identities[name],
            unavailable_reason=reason,
            window_start_mono_ns=start_ns,
            window_end_mono_ns=end_ns,
            boot_id=boot_id,
        )
