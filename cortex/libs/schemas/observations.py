"""Canonical sensor-observation contracts.

The scheduler owns observation identity and time.  Estimators fill in the
value, validity and quality without erasing scheduled-but-missing samples.
That distinction is essential for exposure, coverage and gap calculations.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ObservationSource(StrEnum):
    """Closed catalog of sensor/source families."""

    CAMERA = "camera"
    MOUSE = "mouse"
    KEYBOARD = "keyboard"
    BROWSER = "browser"
    EDITOR = "editor"
    WINDOW = "window"


class ObservationValidity(StrEnum):
    """Whether a scheduled observation contains a usable measurement."""

    VALID = "valid"
    MISSING = "missing"
    REJECTED = "rejected"
    STALE = "stale"


class MissingReason(StrEnum):
    """Closed reason catalog for absent or unusable observations."""

    NO_FACE = "NO_FACE"
    LOW_LIGHT = "LOW_LIGHT"
    SATURATED = "SATURATED"
    MOTION = "MOTION"
    OCCLUDED = "OCCLUDED"
    CAMERA_WARMUP = "CAMERA_WARMUP"
    FRAME_DROPPED = "FRAME_DROPPED"
    PERMISSION = "PERMISSION"
    SOURCE_DISCONNECTED = "SOURCE_DISCONNECTED"
    INSUFFICIENT_WINDOW = "INSUFFICIENT_WINDOW"
    ARTIFACT = "ARTIFACT"
    UNKNOWN = "UNKNOWN"


class CameraIdentity(BaseModel):
    """Non-secret identity of the physical/logical camera in use.

    ``identity_key`` is derived from the live post-open device name and
    source classification, not the AVFoundation index.  Device indices can
    reorder whenever Continuity Camera appears or disappears.
    """

    model_config = ConfigDict(extra="forbid")

    identity_key: str = Field(..., min_length=1)
    device_id: int = Field(..., ge=0, description="Current backend index; not stable identity")
    device_name: str | None = None
    source: str = Field(..., min_length=1)
    backend: int | None = None
    width: int = Field(..., ge=1)
    height: int = Field(..., ge=1)


class CameraFrameObservation(BaseModel):
    """Serializable metadata for one camera-derived observation.

    Raw pixels and landmarks remain process-local and are deliberately not
    part of the wire/schema object.
    """

    model_config = ConfigDict(extra="forbid")

    width: int = Field(..., ge=1)
    height: int = Field(..., ge=1)
    face_detected: bool
    face_stable: bool
    face_confidence: float = Field(..., ge=0.0, le=1.0)
    detector_replayed: bool = False
    detector_timestamp_adjusted: bool = False
    motion_face_widths_per_second: float | None = Field(None, ge=0.0)
    camera_identity: CameraIdentity


ObservationValue = TypeVar("ObservationValue")


class ObservationEnvelope(BaseModel, Generic[ObservationValue]):
    """One scheduled source observation, including explicit missingness."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    schema_version: str = "2.0"
    source: ObservationSource
    source_instance_id: UUID
    sequence: int = Field(..., ge=0)
    observed_at_unix_ms: int = Field(..., ge=0)
    observed_at_mono_ns: int = Field(..., ge=0)
    boot_id: UUID
    value: ObservationValue | None
    validity: ObservationValidity
    missing_reason: MissingReason | None = None
    quality: float = Field(..., ge=0.0, le=1.0)
    quality_components: dict[str, float] = Field(default_factory=dict)
    algorithm_version: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def _validate_value_and_reason(self) -> ObservationEnvelope[ObservationValue]:
        is_valid = self.validity == ObservationValidity.VALID.value
        if is_valid:
            if self.value is None:
                raise ValueError("valid observations must contain a value")
            if self.missing_reason is not None:
                raise ValueError("valid observations cannot contain a missing_reason")
        else:
            if self.value is not None:
                raise ValueError("non-valid observations cannot contain a value")
            if self.missing_reason is None:
                raise ValueError("non-valid observations require a missing_reason")

        invalid_components = {
            name: value
            for name, value in self.quality_components.items()
            if not 0.0 <= value <= 1.0
        }
        if invalid_components:
            raise ValueError(
                "quality component values must be within [0, 1]: "
                f"{sorted(invalid_components)}"
            )
        return self


class CameraObservationEnvelope(ObservationEnvelope[CameraFrameObservation]):
    """Concrete camera specialization emitted by cross-client codegen."""

