"""Composition boundary for the v2 physiology signal pipeline."""

from __future__ import annotations

from cortex.libs.config.settings import RPPGSignalConfig
from cortex.services.physio_engine.v2.backends import (
    BackendValidationError,
    RPPGBackendRegistry,
)
from cortex.services.physio_engine.v2.pulse import PulsePipelineV2
from cortex.services.physio_engine.v2.respiration import RespirationFusionV2


class PhysiologyEngineV2:
    """Own one validated fixed backend and all dependent temporal state."""

    def __init__(
        self,
        config: RPPGSignalConfig,
        *,
        registry: RPPGBackendRegistry | None = None,
        max_motion_face_widths_per_second: float | None = None,
    ) -> None:
        """Build the pulse and respiration pipelines for one fixed backend.

        ``max_motion_face_widths_per_second`` is the window-mean facial
        translation speed above which a pulse window is not published; pass
        ``CaptureConfig.max_motion_face_widths_per_second`` so the window gate
        agrees with the per-frame motion gate.  ``None`` keeps the pulse
        pipeline default.
        """
        if config.dynamic_backend_selection:
            raise BackendValidationError(
                "dynamic rPPG backend selection has no held-out validation artifact; "
                "configure a fixed backend"
            )
        backend_registry = registry or RPPGBackendRegistry.with_packaged_backends()
        backend = backend_registry.resolve(
            config.backend,
            expected_implementation_sha256=config.backend_expected_sha256,
        )
        pulse_kwargs: dict[str, float] = {}
        if max_motion_face_widths_per_second is not None:
            pulse_kwargs["max_motion_face_widths_per_second"] = float(
                max_motion_face_widths_per_second
            )
        self.pulse = PulsePipelineV2(
            backend,
            low_hz=config.bandpass_low,
            high_hz=config.bandpass_high,
            filter_order=config.bandpass_order,
            max_head_jitter_deg=config.max_head_jitter_deg,
            # Signal-presence thresholds. These existed on ``RPPGSignalConfig``
            # from the start but were only ever read by the legacy shadow
            # estimator, so the published v2 path ran with no test for whether
            # a cardiac signal was present at all.
            nsqi_threshold=config.nsqi_threshold,
            min_cardiac_snr_db=config.min_cardiac_snr_db,
            min_peak_concentration=config.min_peak_concentration,
            minimum_window_quality=config.minimum_window_quality,
            experimental_hrv_enabled=config.experimental_hrv_enabled,
            hrv_min_window_seconds=float(config.hrv_min_window_seconds),
            hrv_min_valid_ibi=config.hrv_min_valid_ibi,
            **pulse_kwargs,
        )
        self.respiration = RespirationFusionV2(
            backend,
            low_hz=config.respiration_low_hz,
            high_hz=config.respiration_high_hz,
            min_window_seconds=float(config.respiration_window_seconds),
            minimum_channel_quality=config.respiration_min_channel_quality,
            max_channel_disagreement_bpm=(
                config.respiration_max_channel_disagreement_bpm
            ),
            experimental_publication_enabled=(
                config.experimental_respiration_enabled
            ),
        )

    def reset(self) -> None:
        self.pulse.reset()
