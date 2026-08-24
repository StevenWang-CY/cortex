"""Immutable calibration profiles and live-update events.

Calibration is evidence, not a bag of convenient defaults.  A profile names
the task under which each metric was observed, its sampling/quality limits,
the exact algorithm identity, the camera geometry it is bound to, and whether
the metric is mature enough to influence production behavior.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cortex.libs.schemas.physiology import SignalAlgorithmIdentity
from cortex.libs.schemas.state import UserBaselines

CALIBRATION_PROTOCOL_VERSION = "calibration/2.0.0"
CALIBRATION_FEATURE_SCHEMA_VERSION = "features/2.0"


class CalibrationProvenance(StrEnum):
    """Origin of calibration observations."""

    MEASURED = "measured"
    DEMO = "demo"


class CalibrationReferenceTask(StrEnum):
    """Protocol context in which a metric was acquired."""

    CAMERA_QUALITY_CHECK = "camera_quality_check"
    PHYSIOLOGICAL_REST = "physiological_rest"
    REPRESENTATIVE_WORK = "representative_work"
    NEUTRAL_HEAD_POSE = "neutral_head_pose"


class CalibrationMetricMaturity(StrEnum):
    """Whether a measurement may influence production behavior."""

    OBSERVED = "observed"
    SUPPORTED = "supported"
    EXPERIMENTAL = "experimental"
    UNAVAILABLE = "unavailable"


class CalibrationMetricName(StrEnum):
    HEART_RATE_BPM = "heart_rate_bpm"
    RESPIRATION_RATE_BPM = "respiration_rate_bpm"
    BLINK_RATE_PER_MIN = "blink_rate_per_min"
    OPEN_EYE_RATIO = "open_eye_ratio"
    MOUSE_VELOCITY_PX_PER_S = "mouse_velocity_px_per_s"
    MOUSE_VELOCITY_VARIANCE = "mouse_velocity_variance"
    NEUTRAL_HEAD_PITCH_DEG = "neutral_head_pitch_deg"
    NEUTRAL_FACE_SCALE_PX = "neutral_face_scale_px"


class CalibrationDistribution(BaseModel):
    """Finite distribution summary without retaining raw observations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mean: float
    std: float = Field(..., ge=0.0)
    p10: float
    median: float
    p90: float

    @model_validator(mode="after")
    def _finite_and_ordered(self) -> CalibrationDistribution:
        values = (self.mean, self.std, self.p10, self.median, self.p90)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("calibration distributions must be finite")
        if not self.p10 <= self.median <= self.p90:
            raise ValueError("calibration percentiles must be ordered")
        return self


class CalibrationMetricSummary(BaseModel):
    """Evidence and provenance for one persisted calibration metric."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        use_enum_values=True,
    )

    metric: CalibrationMetricName
    unit: str = Field(..., min_length=1)
    reference_task: CalibrationReferenceTask
    maturity: CalibrationMetricMaturity
    value: float | None = None
    distribution: CalibrationDistribution | None = None
    sample_count: int = Field(..., ge=0)
    effective_sample_count: float = Field(..., ge=0.0)
    valid_duration_seconds: float = Field(..., ge=0.0)
    missing_fraction: float = Field(..., ge=0.0, le=1.0)
    quality_p10: float = Field(..., ge=0.0, le=1.0)
    quality_median: float = Field(..., ge=0.0, le=1.0)
    quality_p90: float = Field(..., ge=0.0, le=1.0)
    algorithm: SignalAlgorithmIdentity
    unavailable_reason: str | None = None

    @model_validator(mode="after")
    def _evidence_is_coherent(self) -> CalibrationMetricSummary:
        if self.effective_sample_count > self.sample_count:
            raise ValueError("effective sample count cannot exceed sample count")
        if not self.quality_p10 <= self.quality_median <= self.quality_p90:
            raise ValueError("calibration quality percentiles must be ordered")
        unavailable = self.maturity == CalibrationMetricMaturity.UNAVAILABLE.value
        if unavailable:
            if self.value is not None or self.distribution is not None:
                raise ValueError("unavailable calibration metrics cannot carry a value")
            if not self.unavailable_reason:
                raise ValueError("unavailable calibration metrics require a reason")
        else:
            if self.value is None or self.distribution is None:
                raise ValueError("available calibration metrics require a distribution")
            if not math.isfinite(self.value):
                raise ValueError("calibration metric values must be finite")
            if self.unavailable_reason is not None:
                raise ValueError("available metrics cannot carry an unavailable reason")
        return self


class CalibrationCameraIdentity(BaseModel):
    """Stable camera and geometry binding; backend indices are excluded."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    identity_key: str = Field(..., min_length=1)
    device_name: str | None = None
    source: str = Field(..., min_length=1)
    width: int = Field(..., ge=1)
    height: int = Field(..., ge=1)


class CalibrationBaselineValues(BaseModel):
    """Named profile values; absence remains absence rather than a default."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    heart_rate_bpm: float | None = Field(None, ge=30.0, le=220.0)
    respiration_rate_bpm: float | None = Field(None, ge=0.0, le=60.0)
    blink_rate_per_min: float | None = Field(None, ge=0.0, le=120.0)
    open_eye_ratio: float | None = Field(None, gt=0.0, lt=1.0)
    mouse_velocity_px_per_s: float | None = Field(None, ge=0.0)
    mouse_velocity_variance: float | None = Field(None, ge=0.0)
    neutral_head_pitch_deg: float | None = Field(None, ge=-180.0, le=180.0)
    neutral_face_scale_px: float | None = Field(None, gt=0.0)


class CalibrationProfile(BaseModel):
    """One immutable, versioned calibration artifact."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        use_enum_values=True,
    )

    schema_version: Literal["2.0"] = "2.0"
    profile_id: UUID
    provenance: CalibrationProvenance
    created_at_unix_ms: int = Field(..., ge=0)
    approved_at_unix_ms: int | None = Field(None, ge=0)
    feature_schema_version: str = Field(..., min_length=1)
    protocol_version: str = Field(..., min_length=1)
    camera: CalibrationCameraIdentity | None = None
    metrics: tuple[CalibrationMetricSummary, ...]
    baselines: CalibrationBaselineValues
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _profile_is_coherent(self) -> CalibrationProfile:
        names = [str(metric.metric) for metric in self.metrics]
        if len(names) != len(set(names)):
            raise ValueError("calibration profile metrics must be unique")
        if (
            self.approved_at_unix_ms is not None
            and self.approved_at_unix_ms < self.created_at_unix_ms
        ):
            raise ValueError("profile approval cannot precede creation")
        if self.provenance == CalibrationProvenance.MEASURED.value and self.camera is None:
            raise ValueError("measured calibration profiles require camera identity")
        if self.provenance == CalibrationProvenance.DEMO.value and self.is_approved:
            raise ValueError("demo calibration profiles cannot be approved")

        baseline_by_metric = {
            CalibrationMetricName.HEART_RATE_BPM.value: self.baselines.heart_rate_bpm,
            CalibrationMetricName.RESPIRATION_RATE_BPM.value: (
                self.baselines.respiration_rate_bpm
            ),
            CalibrationMetricName.BLINK_RATE_PER_MIN.value: (
                self.baselines.blink_rate_per_min
            ),
            CalibrationMetricName.OPEN_EYE_RATIO.value: self.baselines.open_eye_ratio,
            CalibrationMetricName.MOUSE_VELOCITY_PX_PER_S.value: (
                self.baselines.mouse_velocity_px_per_s
            ),
            CalibrationMetricName.MOUSE_VELOCITY_VARIANCE.value: (
                self.baselines.mouse_velocity_variance
            ),
            CalibrationMetricName.NEUTRAL_HEAD_PITCH_DEG.value: (
                self.baselines.neutral_head_pitch_deg
            ),
            CalibrationMetricName.NEUTRAL_FACE_SCALE_PX.value: (
                self.baselines.neutral_face_scale_px
            ),
        }
        summaries = {str(metric.metric): metric for metric in self.metrics}
        for metric_name, baseline_value in baseline_by_metric.items():
            summary = summaries.get(metric_name)
            if baseline_value is None:
                if summary is not None and summary.value is not None:
                    raise ValueError(
                        f"available metric {metric_name} requires a matching baseline"
                    )
                continue
            if summary is None or summary.value is None:
                raise ValueError(
                    f"baseline {metric_name} requires available metric evidence"
                )
            if not math.isclose(
                baseline_value,
                summary.value,
                rel_tol=1e-9,
                abs_tol=1e-9,
            ):
                raise ValueError(
                    f"baseline {metric_name} does not match metric evidence"
                )
        return self

    @property
    def is_demo(self) -> bool:
        return self.provenance == CalibrationProvenance.DEMO.value

    @property
    def is_approved(self) -> bool:
        return self.approved_at_unix_ms is not None

    def metric(self, name: CalibrationMetricName | str) -> CalibrationMetricSummary | None:
        target = str(name)
        return next((item for item in self.metrics if str(item.metric) == target), None)

    def to_user_baselines(self) -> UserBaselines:
        """Build the legacy scorer view without promoting experimental metrics."""

        values: dict[str, object] = {
            "calibrated_at": datetime.fromtimestamp(
                self.created_at_unix_ms / 1000.0,
                tz=UTC,
            ),
            "metric_distributions": {},
        }
        distributions: dict[str, dict[str, float]] = {}
        mapping = {
            CalibrationMetricName.HEART_RATE_BPM.value: "hr",
            CalibrationMetricName.RESPIRATION_RATE_BPM.value: "resp_rate",
            CalibrationMetricName.BLINK_RATE_PER_MIN.value: "blink_rate",
            CalibrationMetricName.MOUSE_VELOCITY_PX_PER_S.value: "mouse_velocity",
            CalibrationMetricName.MOUSE_VELOCITY_VARIANCE.value: "mouse_variance",
        }
        for summary in self.metrics:
            if summary.distribution is None:
                continue
            maturity = str(summary.maturity)
            metric_name = str(summary.metric)
            if maturity not in {
                CalibrationMetricMaturity.OBSERVED.value,
                CalibrationMetricMaturity.SUPPORTED.value,
            }:
                continue
            if (
                metric_name
                in {
                    CalibrationMetricName.HEART_RATE_BPM.value,
                    CalibrationMetricName.RESPIRATION_RATE_BPM.value,
                }
                and maturity != CalibrationMetricMaturity.SUPPORTED.value
            ):
                # Experimental camera physiology is evidence for review, not
                # an input to the legacy production scorer or attribution path.
                continue
            legacy_name = mapping.get(metric_name)
            if legacy_name is not None:
                distributions[legacy_name] = {
                    "mu": summary.distribution.mean,
                    "sigma": summary.distribution.std,
                    "std": summary.distribution.std,
                    "p10": summary.distribution.p10,
                    "p90": summary.distribution.p90,
                }
        values["metric_distributions"] = distributions

        heart = self.metric(CalibrationMetricName.HEART_RATE_BPM)
        respiration = self.metric(CalibrationMetricName.RESPIRATION_RATE_BPM)
        if (
            self.baselines.heart_rate_bpm is not None
            and heart is not None
            and heart.maturity == CalibrationMetricMaturity.SUPPORTED.value
        ):
            values["hr_baseline"] = self.baselines.heart_rate_bpm
            if heart.distribution is not None:
                values["hr_std"] = max(1.0, min(20.0, heart.distribution.std))
        if (
            self.baselines.respiration_rate_bpm is not None
            and respiration is not None
            and respiration.maturity == CalibrationMetricMaturity.SUPPORTED.value
        ):
            values["resp_baseline"] = self.baselines.respiration_rate_bpm
        if self.baselines.blink_rate_per_min is not None:
            values["blink_rate_baseline"] = self.baselines.blink_rate_per_min
        if self.baselines.mouse_velocity_px_per_s is not None:
            values["mouse_velocity_baseline"] = self.baselines.mouse_velocity_px_per_s
        if self.baselines.mouse_velocity_variance is not None:
            values["mouse_variance_baseline"] = self.baselines.mouse_velocity_variance
        return UserBaselines.model_validate(values)


class ActiveCalibrationPointer(BaseModel):
    """Atomic pointer to the one active immutable measured profile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    profile_id: UUID
    profile_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    activated_at_unix_ms: int = Field(..., ge=0)


class CalibrationUpdated(BaseModel):
    """Domain event emitted after all dependent services switch together."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    event_id: UUID
    profile_id: UUID
    previous_profile_id: UUID | None = None
    observed_at_unix_ms: int = Field(..., ge=0)
    observed_at_mono_ns: int = Field(..., ge=0)
    boot_id: UUID
    camera_calibration_valid: bool
    applied_metrics: tuple[str, ...]
    reset_components: tuple[str, ...]
