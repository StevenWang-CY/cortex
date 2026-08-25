"""Evidence-bearing contracts for physiological signal processing.

These models deliberately separate an estimate from the evidence required to
interpret it.  A number without algorithm identity, time bounds, quality and
publication status is not a valid Cortex physiology result.
"""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EvidenceStatus(StrEnum):
    """Publication/readiness state of a physiological estimate."""

    SUPPORTED = "supported"
    EXPERIMENTAL = "experimental"
    UNAVAILABLE = "unavailable"
    REJECTED = "rejected"


class PhysiologyMetric(StrEnum):
    """Closed metric catalog; unsupported metrics remain explicit."""

    HEART_RATE = "heart_rate"
    RMSSD = "rmssd"
    SDNN = "sdnn"
    PNN50 = "pnn50"
    SD1 = "sd1"
    SD2 = "sd2"
    LF_HF_RATIO = "lf_hf_ratio"
    SAMPLE_ENTROPY = "sample_entropy"
    RESPIRATION_RATE = "respiration_rate"


class BeatStatus(StrEnum):
    """Lifecycle of a candidate in the overlap-reconciled beat ledger."""

    PROVISIONAL = "provisional"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class BeatRejectionReason(StrEnum):
    """Closed reasons for excluding a beat or derived interval."""

    LOW_QUALITY = "low_quality"
    REFRACTORY_CONFLICT = "refractory_conflict"
    WINDOW_BOUNDARY = "window_boundary"
    IBI_TOO_SHORT = "ibi_too_short"
    IBI_TOO_LONG = "ibi_too_long"
    IBI_LOCAL_OUTLIER = "ibi_local_outlier"


class SignalAlgorithmIdentity(BaseModel):
    """Exact implementation and optional model asset used for an estimate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(..., min_length=1)
    version: str = Field(..., min_length=1)
    implementation_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    asset_sha256: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")
    configuration_sha256: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")
    parameters: dict[str, str | int | float | bool] = Field(default_factory=dict)
    selection_mode: Literal["fixed", "validated_dynamic"] = "fixed"

    @model_validator(mode="after")
    def _parameters_are_finite(self) -> SignalAlgorithmIdentity:
        if any(not key for key in self.parameters):
            raise ValueError("algorithm parameter names must be non-empty")
        for name, value in self.parameters.items():
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"algorithm parameter {name!r} must be finite")
        return self


class EstimateUncertainty(BaseModel):
    """Bounded interval around a numeric estimate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    lower: float
    upper: float
    confidence_level: float = Field(..., gt=0.0, lt=1.0)
    method: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def _ordered(self) -> EstimateUncertainty:
        if not math.isfinite(self.lower) or not math.isfinite(self.upper):
            raise ValueError("uncertainty bounds must be finite")
        if self.lower > self.upper:
            raise ValueError("uncertainty lower bound must not exceed upper bound")
        return self


class SignalEstimate(BaseModel):
    """One metric estimate with its evidence and release status."""

    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    metric: PhysiologyMetric
    value: float | None = None
    unit: str = Field(..., min_length=1)
    status: EvidenceStatus
    quality: float = Field(..., ge=0.0, le=1.0)
    algorithm: SignalAlgorithmIdentity
    uncertainty: EstimateUncertainty | None = None
    unavailable_reason: str | None = None
    window_start_mono_ns: int = Field(..., ge=0)
    window_end_mono_ns: int = Field(..., ge=0)
    boot_id: UUID

    @model_validator(mode="after")
    def _status_matches_value(self) -> SignalEstimate:
        if self.window_end_mono_ns < self.window_start_mono_ns:
            raise ValueError("estimate window must be ordered")
        unavailable = self.status in {
            EvidenceStatus.UNAVAILABLE.value,
            EvidenceStatus.REJECTED.value,
        }
        if unavailable and self.value is not None:
            raise ValueError("unavailable/rejected estimates cannot carry a value")
        if not unavailable and self.value is None:
            raise ValueError("supported/experimental estimates require a value")
        if unavailable and not self.unavailable_reason:
            raise ValueError("unavailable/rejected estimates require a reason")
        if not unavailable and self.unavailable_reason is not None:
            raise ValueError("available estimates cannot carry an unavailable reason")
        if self.value is not None:
            if not math.isfinite(self.value):
                raise ValueError("estimate values must be finite")
            bounds = {
                PhysiologyMetric.HEART_RATE.value: (30.0, 220.0),
                PhysiologyMetric.RESPIRATION_RATE.value: (0.0, 60.0),
            }
            lower, upper = bounds.get(str(self.metric), (0.0, float("inf")))
            if not lower <= self.value <= upper:
                raise ValueError(f"{self.metric} value falls outside its contract")
            if self.uncertainty is not None and not (
                self.uncertainty.lower <= self.value <= self.uncertainty.upper
            ):
                raise ValueError("estimate uncertainty must contain the value")
        return self


class BeatCandidate(BaseModel):
    """Peak observation located on the process monotonic clock."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(..., min_length=1)
    absolute_mono_ns: int = Field(..., ge=0)
    prominence: float = Field(..., ge=0.0)
    quality: float = Field(..., ge=0.0, le=1.0)
    source_window_id: str = Field(..., min_length=1)
    near_window_boundary: bool = False


class BeatEvent(BaseModel):
    """Canonical, overlap-reconciled beat and its provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    beat_id: str = Field(..., min_length=1)
    absolute_mono_ns: int = Field(..., ge=0)
    status: BeatStatus
    rejection_reason: BeatRejectionReason | None = None
    quality: float = Field(..., ge=0.0, le=1.0)
    prominence: float = Field(..., ge=0.0)
    source_window_ids: tuple[str, ...] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _rejection_is_explicit(self) -> BeatEvent:
        rejected = self.status == BeatStatus.REJECTED.value
        if rejected != (self.rejection_reason is not None):
            raise ValueError("only rejected beats carry a rejection reason")
        if len(set(self.source_window_ids)) != len(self.source_window_ids):
            raise ValueError("beat provenance cannot contain duplicate windows")
        return self


class InterBeatInterval(BaseModel):
    """Interval derived only from two named canonical beats."""

    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    ibi_id: str = Field(..., min_length=1)
    left_beat_id: str = Field(..., min_length=1)
    right_beat_id: str = Field(..., min_length=1)
    start_mono_ns: int = Field(..., ge=0)
    end_mono_ns: int = Field(..., ge=0)
    duration_ms: float = Field(..., gt=0.0)
    status: BeatStatus
    rejection_reason: BeatRejectionReason | None = None
    correction: Literal["none"] = "none"
    quality: float = Field(..., ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate_interval(self) -> InterBeatInterval:
        if self.end_mono_ns <= self.start_mono_ns:
            raise ValueError("IBI endpoints must be strictly ordered")
        rejected = self.status == BeatStatus.REJECTED.value
        if rejected != (self.rejection_reason is not None):
            raise ValueError("only rejected intervals carry a rejection reason")
        expected_ms = (self.end_mono_ns - self.start_mono_ns) / 1_000_000.0
        if not math.isclose(self.duration_ms, expected_ms, abs_tol=1e-6):
            raise ValueError("IBI duration must equal its absolute beat endpoints")
        return self


class PulseWindowSummary(BaseModel):
    """Serializable summary of a processed window (waveform stays local)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    window_id: str = Field(..., min_length=1)
    boot_id: UUID
    window_start_mono_ns: int = Field(..., ge=0)
    window_end_mono_ns: int = Field(..., ge=0)
    sample_rate_hz: float = Field(..., gt=0.0)
    sample_count: int = Field(..., ge=2)
    algorithm: SignalAlgorithmIdentity
    quality: float = Field(..., ge=0.0, le=1.0)
    hr: SignalEstimate
    candidate_count: int = Field(..., ge=0)
    accepted_beat_count: int = Field(..., ge=0)
    provisional_beat_count: int = Field(..., ge=0)
    rejected_beat_count: int = Field(..., ge=0)

    @model_validator(mode="after")
    def _evidence_matches_window(self) -> PulseWindowSummary:
        if self.window_end_mono_ns <= self.window_start_mono_ns:
            raise ValueError("pulse window must be strictly ordered")
        if (
            self.hr.window_start_mono_ns != self.window_start_mono_ns
            or self.hr.window_end_mono_ns != self.window_end_mono_ns
            or self.hr.boot_id != self.boot_id
        ):
            raise ValueError("pulse estimate clock domain must match its window")
        if self.hr.algorithm != self.algorithm:
            raise ValueError("pulse estimate algorithm must match its window")
        return self
