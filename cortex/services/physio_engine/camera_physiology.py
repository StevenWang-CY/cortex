"""The product path from camera observations to physiology features.

The runtime daemon and the qualification harness both call this module, so
the numbers a qualification report scores are produced by the code that runs
in the app.  Nothing here touches the camera, the clock or the network: the
inputs are observations that already carry their own time and quality.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import numpy as np
from numpy.typing import NDArray

from cortex.libs.config.settings import RPPGSignalConfig
from cortex.libs.schemas.features import PhysioFeatures
from cortex.libs.schemas.observations import MissingReason, ObservationValidity
from cortex.libs.schemas.physiology import PhysiologyMetric, SignalEstimate
from cortex.services.capture_service.observation_buffer import (
    NumericObservation,
    ObservationBuffer,
    PreparedObservationWindow,
    prepare_observation_window,
)
from cortex.services.physio_engine.v2.engine import PhysiologyEngineV2
from cortex.services.physio_engine.v2.pulse import PulseProcessingResult
from cortex.services.physio_engine.v2.respiration import RespirationProcessingResult

# Heart-rate change is reported against the estimate from about this long ago.
HR_DELTA_LOOKBACK_SECONDS = 5.0
HR_DELTA_TOLERANCE_SECONDS = 2.5


def face_normalized_vertical_position(landmarks_px: Any) -> float | None:
    """Face-centre vertical position in units of observed face height.

    Pixel displacement depends on camera resolution and distance.  Dividing
    by the current face height yields a dimensionless motion channel suitable
    for detrending; degenerate landmark sets abstain explicitly.
    """

    try:
        landmarks = np.asarray(landmarks_px, dtype=np.float64)
    except (TypeError, ValueError):
        return None
    if (
        landmarks.ndim != 2
        or landmarks.shape[0] < 3
        or landmarks.shape[1] < 2
        or not bool(np.isfinite(landmarks[:, :2]).all())
    ):
        return None
    vertical = landmarks[:, 1]
    lower, upper = np.percentile(vertical, [5.0, 95.0])
    face_height = float(upper - lower)
    if face_height < 1.0:
        return None
    return float(np.median(vertical) / face_height)


def numeric_observation_from_capture(
    *,
    observation: Any,
    frame: NDArray[np.uint8] | None,
    landmarks_px: NDArray[np.float32] | None,
    roi_extractor: Any,
) -> NumericObservation:
    """Reduce one capture observation to the RGB sample the engine consumes.

    A frame contributes a value only when the capture service marked it valid
    *and* a finite skin-region mean could be extracted from it.  Everything
    else becomes an explicit invalid sample, so a window's coverage counts it
    as missing rather than interpolating through it silently.
    """

    validity = str(observation.validity)
    missing_reason = observation.missing_reason
    rgb_value: NDArray[np.float64] | None = None
    motion: float | None = None
    if (
        validity == ObservationValidity.VALID.value
        and frame is not None
        and landmarks_px is not None
    ):
        roi_frame = roi_extractor.extract(
            frame,
            landmarks_px,
            observation.observed_at_unix_ms / 1000.0,
        )
        combined = roi_frame.combined_rgb()
        if combined is not None and bool(np.isfinite(combined).all()):
            rgb_value = np.asarray(combined, dtype=np.float64)
            motion = getattr(observation.value, "motion_face_widths_per_second", None)
        else:
            validity = ObservationValidity.REJECTED.value
            missing_reason = MissingReason.OCCLUDED
    elif validity == ObservationValidity.VALID.value:
        validity = ObservationValidity.REJECTED.value
        missing_reason = MissingReason.ARTIFACT

    return NumericObservation(
        observed_at_unix_ms=observation.observed_at_unix_ms,
        observed_at_mono_ns=observation.observed_at_mono_ns,
        boot_id=observation.boot_id,
        sequence=observation.sequence,
        value=rgb_value,
        validity=validity,
        missing_reason=missing_reason,
        quality=observation.quality if rgb_value is not None else 0.0,
        motion_face_widths_per_second=motion,
        head_vertical_face_units=(
            face_normalized_vertical_position(landmarks_px)
            if rgb_value is not None and landmarks_px is not None
            else None
        ),
    )


def build_rgb_observation_buffer(
    config: RPPGSignalConfig,
    *,
    minimum_items: int = 0,
) -> ObservationBuffer[NumericObservation]:
    """Bounded camera-signal history long enough for the slowest estimator."""

    window_seconds = max(
        float(config.window_seconds),
        float(config.long_window_seconds),
        float(config.respiration_window_seconds),
    )
    max_age_seconds = window_seconds + config.max_interpolation_gap_ms / 1000.0 + 1.0
    return ObservationBuffer(
        max_age_seconds=max_age_seconds,
        max_items=max(
            int(minimum_items),
            int(max_age_seconds * config.fps_clamp_max) + 2,
        ),
    )


@dataclass(frozen=True)
class PhysiologyUpdate:
    """Everything one stride of the physiology engine produced."""

    features: PhysioFeatures
    prepared: PreparedObservationWindow
    pulse: PulseProcessingResult | None
    respiration: RespirationProcessingResult | None

    @property
    def window_ready(self) -> bool:
        return self.pulse is not None


def _published(estimate: SignalEstimate | None) -> float | None:
    if estimate is None or estimate.value is None:
        return None
    return float(estimate.value)


class CameraPhysiologyProcessor:
    """Turn a buffer of RGB observations into one :class:`PhysioFeatures`.

    The processor owns the engine's temporal state (beat ledger, heart-rate
    prior, recent heart-rate history).  Call :meth:`reset` whenever that
    history stops describing the same person in front of the same camera.
    """

    def __init__(
        self,
        config: RPPGSignalConfig,
        *,
        nominal_fps: float,
        engine: PhysiologyEngineV2,
    ) -> None:
        if nominal_fps <= 0:
            raise ValueError("nominal_fps must be positive")
        self._config = config
        self._nominal_fps = float(nominal_fps)
        self._engine = engine
        self._recent_hr: deque[tuple[int, float]] = deque(maxlen=64)
        self._latest_respiration: SignalEstimate | None = None

    @property
    def engine(self) -> PhysiologyEngineV2:
        return self._engine

    @property
    def latest_respiration(self) -> SignalEstimate | None:
        return self._latest_respiration

    def reset(self) -> None:
        self._engine.reset()
        self._recent_hr.clear()
        self._latest_respiration = None

    def prepare_pulse_window(
        self, observations: Sequence[NumericObservation]
    ) -> PreparedObservationWindow:
        """The longest pulse window the buffer can currently fill.

        The long window is preferred: it resolves the cardiac line better.
        Until it has filled (the first seconds of a session, or after a gap)
        the short window gives a first reading sooner.
        """

        short = float(self._config.window_seconds)
        long = float(self._config.long_window_seconds)
        if long > short:
            prepared = self._prepare(observations, long)
            if prepared.ready:
                return prepared
        return self._prepare(observations, short)

    def prepare_respiration_window(
        self, observations: Sequence[NumericObservation]
    ) -> PreparedObservationWindow:
        return self._prepare(
            observations, float(self._config.respiration_window_seconds)
        )

    def _prepare(
        self, observations: Sequence[NumericObservation], window_seconds: float
    ) -> PreparedObservationWindow:
        cfg = self._config
        return prepare_observation_window(
            tuple(observations),
            window_seconds=window_seconds,
            nominal_fps=self._nominal_fps,
            min_valid_fraction=cfg.min_valid_coverage,
            max_interpolation_gap_ms=cfg.max_interpolation_gap_ms,
            max_motion_fraction=cfg.max_motion_rejected_fraction,
            fps_clamp_min=cfg.fps_clamp_min,
            fps_clamp_max=cfg.fps_clamp_max,
        )

    def process(
        self,
        observations: Sequence[NumericObservation],
        *,
        boot_id: UUID,
    ) -> PhysiologyUpdate:
        """Run one stride over the newest observations."""

        prepared = self.prepare_pulse_window(observations)
        if (
            not prepared.ready
            or prepared.values is None
            or prepared.sample_times_mono_ns is None
        ):
            return PhysiologyUpdate(
                features=PhysioFeatures(
                    pulse_bpm=None,
                    pulse_quality=prepared.quality,
                    pulse_variability_proxy=None,
                    physio_sqi=prepared.quality,
                    physio_sqi_components={
                        "valid_fraction": prepared.valid_fraction,
                        "temporal_coverage": prepared.temporal_coverage,
                        "artifact_fraction": prepared.artifact_fraction,
                    },
                    hr_delta_5s=None,
                    valid=False,
                ),
                prepared=prepared,
                pulse=None,
                respiration=None,
            )

        pulse = self._engine.pulse.process_window(
            prepared.values,
            prepared.sample_times_mono_ns,
            sample_rate_hz=prepared.sample_rate_hz,
            boot_id=boot_id,
            observation_quality=prepared.quality,
            motion_face_widths_per_second=prepared.mean_motion_face_widths_per_second,
            face_presence_ratio=prepared.valid_fraction,
        )

        respiration: RespirationProcessingResult | None = None
        respiration_window = self.prepare_respiration_window(observations)
        if (
            respiration_window.ready
            and respiration_window.values is not None
            and respiration_window.sample_times_mono_ns is not None
        ):
            respiration = self._engine.respiration.process_window(
                respiration_window.values,
                respiration_window.sample_times_mono_ns,
                sample_rate_hz=respiration_window.sample_rate_hz,
                boot_id=boot_id,
                head_vertical_face_units=respiration_window.head_vertical_face_units,
                beat_intervals=pulse.intervals,
                cardiac_signal_present=pulse.signal_present,
            )
            self._latest_respiration = respiration.fused
        else:
            # A breathing rate describes its own 45 s window.  Once that
            # window can no longer be assembled the previous rate is history,
            # not a reading.
            self._latest_respiration = None

        hr = _published(pulse.summary.hr)
        end_ns = pulse.summary.window_end_mono_ns
        hrv = pulse.hrv_estimates
        rmssd = _published(hrv.get(PhysiologyMetric.RMSSD))
        sdnn = _published(hrv.get(PhysiologyMetric.SDNN))
        respiration_rate = _published(self._latest_respiration)
        # One published measurement is enough for the channel to be live;
        # each field still stands or falls on its own evidence. Only the
        # measurements the rules may read count: RMSSD did not qualify, so a
        # published RMSSD must not be what marks the channel live.
        valid = any(value is not None for value in (hr, sdnn, respiration_rate))

        def _hrv(metric: PhysiologyMetric) -> float | None:
            return _published(hrv.get(metric)) if valid else None

        features = PhysioFeatures(
            pulse_bpm=hr,
            pulse_quality=pulse.summary.quality,
            # RMSSD travels only with a live channel; alone it is not one.
            pulse_variability_proxy=rmssd if valid else None,
            hrv_sdnn=_hrv(PhysiologyMetric.SDNN),
            hrv_pnn50=_hrv(PhysiologyMetric.PNN50),
            hrv_sd1=_hrv(PhysiologyMetric.SD1),
            hrv_sd2=_hrv(PhysiologyMetric.SD2),
            hrv_lf_hf_ratio=_hrv(PhysiologyMetric.LF_HF_RATIO),
            hrv_sample_entropy=_hrv(PhysiologyMetric.SAMPLE_ENTROPY),
            physio_sqi=pulse.summary.quality,
            physio_sqi_components={
                "observation_quality": prepared.quality,
                "valid_fraction": prepared.valid_fraction,
                "artifact_fraction": prepared.artifact_fraction,
                **{
                    key: float(value)
                    for key, value in pulse.sqi_components.items()
                    if np.isfinite(value)
                },
            },
            hr_delta_5s=self._heart_rate_delta(hr, end_ns),
            respiration_rate_bpm=respiration_rate,
            pulse_evidence=pulse.summary.hr,
            hrv_evidence={metric.value: estimate for metric, estimate in hrv.items()},
            respiration_evidence=self._latest_respiration,
            valid=valid,
        )
        return PhysiologyUpdate(
            features=features,
            prepared=prepared,
            pulse=pulse,
            respiration=respiration,
        )

    def _heart_rate_delta(self, hr: float | None, end_ns: int) -> float | None:
        """Change against the published rate about five seconds earlier."""

        if hr is None:
            return None
        target_ns = end_ns - int(HR_DELTA_LOOKBACK_SECONDS * 1_000_000_000)
        tolerance_ns = int(HR_DELTA_TOLERANCE_SECONDS * 1_000_000_000)
        best: tuple[int, float] | None = None
        for stamp_ns, value in self._recent_hr:
            distance = abs(stamp_ns - target_ns)
            if distance <= tolerance_ns and (
                best is None or distance < abs(best[0] - target_ns)
            ):
                best = (stamp_ns, value)
        self._recent_hr.append((end_ns, hr))
        return None if best is None else float(hr - best[1])
