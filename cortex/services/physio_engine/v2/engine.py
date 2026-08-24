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
    ) -> None:
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
        self.pulse = PulsePipelineV2(
            backend,
            low_hz=config.bandpass_low,
            high_hz=config.bandpass_high,
            filter_order=config.bandpass_order,
            max_head_jitter_deg=config.max_head_jitter_deg,
            experimental_hrv_enabled=config.experimental_hrv_enabled,
            hrv_min_window_seconds=float(config.hrv_min_window_seconds),
            hrv_min_valid_ibi=config.hrv_min_valid_ibi,
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
