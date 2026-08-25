"""
Cortex State Schemas

Pydantic models for user state estimation, baselines, and state transitions.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator


class UserState(StrEnum):
    """One-cycle compatibility aliases used by existing clients."""

    UNKNOWN = "UNKNOWN"
    FLOW = "FLOW"  # Compatibility alias for flow_like
    HYPO = "HYPO"  # Compatibility alias for under_engaged
    HYPER = "HYPER"  # Compatibility alias for support_likely
    RECOVERY = "RECOVERY"  # Compatibility alias for recovering


class SupportState(StrEnum):
    """Decision-support target names that avoid diagnostic claims."""

    SUPPORT_LIKELY = "support_likely"
    UNDER_ENGAGED = "under_engaged"
    FLOW_LIKE = "flow_like"
    RECOVERING = "recovering"
    UNKNOWN = "unknown"


class EstimateStatus(StrEnum):
    """Whether the current support estimate is actionable."""

    ESTIMATED = "estimated"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    WARMING_UP = "warming_up"


class SupportScores(BaseModel):
    """Bounded deterministic support scores; these are not probabilities."""

    support_likely: float = Field(0.0, ge=0.0, le=1.0)
    under_engaged: float = Field(0.0, ge=0.0, le=1.0)
    flow_like: float = Field(0.0, ge=0.0, le=1.0)
    recovering: float = Field(0.0, ge=0.0, le=1.0)

    def dominant(self) -> tuple[SupportState, float]:
        values = {
            SupportState.SUPPORT_LIKELY: self.support_likely,
            SupportState.UNDER_ENGAGED: self.under_engaged,
            SupportState.FLOW_LIKE: self.flow_like,
            SupportState.RECOVERING: self.recovering,
        }
        state = max(values, key=lambda candidate: values[candidate])
        score = values[state]
        if score <= 0.0:
            return SupportState.UNKNOWN, 0.0
        return state, score

    def to_legacy(self) -> StateScores:
        return StateScores(
            flow=self.flow_like,
            hypo=self.under_engaged,
            hyper=self.support_likely,
            recovery=self.recovering,
        )


class FeatureContribution(BaseModel):
    """Auditable contribution of one observed or missing feature."""

    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    feature: str = Field(..., min_length=1)
    support_state: SupportState
    direction: Literal["positive", "negative", "missing"]
    contribution: float = Field(..., ge=-1.0, le=1.0)
    quality: float = Field(..., ge=0.0, le=1.0)
    observed: bool
    note: str = Field(..., min_length=1)


class InferenceModelIdentity(BaseModel):
    """Identity and evidence maturity of a support inference implementation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(..., min_length=1)
    version: str = Field(..., min_length=1)
    feature_schema_version: str = Field(..., min_length=1)
    implementation_sha256: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")
    validation_status: Literal[
        "deterministic_rules", "research_only", "validated", "unregistered"
    ]
    probability_calibration_artifact_id: str | None = None


def legacy_inference_identity() -> InferenceModelIdentity:
    """Identity for compatibility callers that construct estimates directly."""

    return InferenceModelIdentity(
        name="legacy-compatibility-input",
        version="unregistered",
        feature_schema_version="legacy-flat-v1",
        implementation_sha256=None,
        validation_status="unregistered",
        probability_calibration_artifact_id=None,
    )


class SignalQuality(BaseModel):
    """Signal quality metrics for each feature channel."""

    physio: float = Field(
        0.0, ge=0.0, le=1.0, description="Physiological signal quality"
    )
    kinematics: float = Field(
        0.0, ge=0.0, le=1.0, description="Kinematic signal quality"
    )
    telemetry: float = Field(
        0.0, ge=0.0, le=1.0, description="Telemetry signal quality"
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def overall(self) -> float:
        """Compute overall signal quality as weighted average."""
        weights = [0.4, 0.3, 0.3]  # Physio weighted higher
        qualities = [self.physio, self.kinematics, self.telemetry]
        return sum(w * q for w, q in zip(weights, qualities, strict=False))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def acceptable(self) -> bool:
        """Check if signal quality is acceptable for intervention."""
        return self.overall >= 0.3


class StateScores(BaseModel):
    """Scores for each possible user state."""

    flow: float = Field(0.0, ge=0.0, le=1.0, description="Legacy flow-like score")
    hypo: float = Field(0.0, ge=0.0, le=1.0, description="Legacy under-engaged score")
    hyper: float = Field(0.0, ge=0.0, le=1.0, description="Legacy support-likely score")
    recovery: float = Field(0.0, ge=0.0, le=1.0, description="Legacy recovering score")

    def dominant_state(self) -> tuple[UserState, float]:
        """Get the dominant state and its score."""
        scores = {
            UserState.FLOW: self.flow,
            UserState.HYPO: self.hypo,
            UserState.HYPER: self.hyper,
            UserState.RECOVERY: self.recovery,
        }
        dominant = max(scores, key=lambda k: scores[k])
        score = scores[dominant]
        if score <= 0.0:
            return UserState.UNKNOWN, 0.0
        return dominant, score


class RuleEvaluation(BaseModel):
    """Stateless Level-A rule output consumed by the temporal smoother."""

    status: EstimateStatus
    scores: SupportScores
    evidence_coverage: float = Field(..., ge=0.0, le=1.0)
    state_coverage: dict[SupportState, float] = Field(default_factory=dict)
    contributing_features: list[FeatureContribution] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    model: InferenceModelIdentity


class StateEstimate(BaseModel):
    """
    Complete state estimation output from the state engine.

    Produced every 500ms from fused feature vectors.
    """

    model_config = ConfigDict(use_enum_values=True)

    estimate_id: UUID = Field(default_factory=uuid4)
    state: UserState = Field(
        ..., description="Deprecated uppercase compatibility alias"
    )
    support_state: SupportState | None = Field(
        None, description="Canonical decision-support state"
    )
    status: EstimateStatus = EstimateStatus.ESTIMATED
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description=(
            "Deprecated compatibility name for bounded evidence strength; "
            "not a probability or diagnostic confidence."
        ),
    )
    scores: StateScores = Field(
        ..., description="Deprecated uppercase projection of heuristic scores"
    )
    support_scores: SupportScores | None = None
    evidence_coverage: float = Field(1.0, ge=0.0, le=1.0)
    contributing_features: list[FeatureContribution] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    model: InferenceModelIdentity = Field(default_factory=legacy_inference_identity)
    probabilities: SupportScores | None = Field(
        None,
        description="Only present for a registered model with a calibration artifact.",
    )
    reasons: list[str] = Field(
        default_factory=list,
        description="Human-readable reasons for current state",
    )
    signal_quality: SignalQuality = Field(
        ..., description="Signal quality per channel"
    )
    timestamp: float = Field(
        ...,
        deprecated=True,
        description=(
            "Deprecated v1 state-pipeline monotonic seconds. Prefer the "
            "explicit observed_at_* fields."
        ),
    )
    schema_version: Literal["2.0"] = "2.0"
    observed_at_unix_ms: int | None = Field(None, ge=0)
    observed_at_mono_ns: int | None = Field(None, ge=0)
    boot_id: UUID | None = None
    dwell_seconds: float = Field(
        0.0, ge=0.0, description="Seconds in current state"
    )
    stress_integral: float | None = Field(
        None,
        ge=0.0,
        description="Compatibility field; unavailable pending reference validation",
    )
    calibrated_probabilities: StateScores | None = Field(
        None,
        deprecated=True,
        description="Deprecated compatibility field; absent for deterministic rules.",
    )
    classifier_source: Literal["rule", "ml", "ensemble"] | None = Field(
        None, description="Classifier source used for this estimate"
    )
    classifier_alpha: float | None = Field(
        None, ge=0.0, le=1.0, description="Ensemble weight on ML branch when used"
    )

    @model_validator(mode="after")
    def _validate_contract(self) -> StateEstimate:
        supplied = (
            self.observed_at_unix_ms is not None,
            self.observed_at_mono_ns is not None,
            self.boot_id is not None,
        )
        if any(supplied) and not all(supplied):
            raise ValueError("state estimate v2 time fields must be supplied together")
        state_to_support = {
            UserState.HYPER.value: SupportState.SUPPORT_LIKELY,
            UserState.HYPO.value: SupportState.UNDER_ENGAGED,
            UserState.FLOW.value: SupportState.FLOW_LIKE,
            UserState.RECOVERY.value: SupportState.RECOVERING,
            UserState.UNKNOWN.value: SupportState.UNKNOWN,
        }
        expected_support = state_to_support[str(self.state)]
        if self.support_state is None:
            self.support_state = expected_support
        elif self.support_state != expected_support:
            raise ValueError("support_state and compatibility state disagree")
        if self.support_scores is None:
            self.support_scores = SupportScores(
                support_likely=self.scores.hyper,
                under_engaged=self.scores.hypo,
                flow_like=self.scores.flow,
                recovering=self.scores.recovery,
            )
        if self.status != EstimateStatus.ESTIMATED and self.state != UserState.UNKNOWN:
            raise ValueError("non-estimated outputs must use UNKNOWN/unknown state")
        # Read the deprecated compatibility slot without triggering Pydantic's
        # access warning inside validation itself.
        legacy_probabilities = self.__dict__.get("calibrated_probabilities")
        has_probability_output = self.probabilities is not None or legacy_probabilities is not None
        if (
            has_probability_output
            and self.model.probability_calibration_artifact_id is None
        ):
            raise ValueError(
                "probability output requires a registered calibration artifact"
            )
        return self

    @property
    def is_overwhelmed(self) -> bool:
        """Compatibility predicate for the HYPER/support-likely alias."""
        return self.state == "HYPER"

    @property
    def is_flow(self) -> bool:
        """Compatibility predicate for the FLOW/flow-like alias."""
        return self.state == "FLOW"

    @property
    def should_intervene(self) -> bool:
        """
        Quick check if intervention conditions might be met.

        Full trigger policy check happens in intervention engine.
        """
        return (
            self.is_overwhelmed
            and self.status == EstimateStatus.ESTIMATED
            and self.support_state == SupportState.SUPPORT_LIKELY
            and self.evidence_coverage >= 0.45
            and self.confidence >= 0.85
            and self.signal_quality.acceptable
        )


class UserBaselines(BaseModel):
    """
    Personal baseline measurements for a user.

    Captured during calibration and used for relative scoring.
    """

    hr_baseline: float = Field(
        72.0, ge=40.0, le=120.0, description="Baseline heart rate (BPM)"
    )
    hr_std: float = Field(
        5.0, ge=1.0, le=20.0, description="Heart rate standard deviation"
    )
    hrv_baseline: float = Field(
        50.0, ge=10.0, le=200.0, description="Baseline RMSSD (ms)"
    )
    blink_rate_baseline: float = Field(
        17.0, ge=5.0, le=30.0, description="Baseline blink rate (blinks/min)"
    )
    mouse_velocity_baseline: float = Field(
        500.0, ge=0.0, description="Baseline mouse velocity (px/s)"
    )
    mouse_variance_baseline: float = Field(
        10000.0, ge=0.0, description="Baseline mouse variance"
    )
    resp_baseline: float = Field(
        15.0, ge=4.0, le=30.0, description="Baseline respiration rate (breaths/min)"
    )
    calibrated_at: datetime | None = Field(
        None, description="When calibration was performed"
    )
    metric_distributions: dict[str, dict[str, float]] = Field(
        default_factory=dict,
        description="Per-metric distribution stats (mu, sigma, p10, p90)",
    )
    circadian_hr_cosinor: dict[str, float] = Field(
        default_factory=dict,
        description="Optional circadian cosinor model params (mesor, amplitude, acrophase)",
    )
    rolling_rebaseline_seconds: float = Field(
        60.0,
        ge=0.0,
        description="Default rolling morning re-baseline capture window in seconds",
    )
    ew_decay_half_life_days: float = Field(
        7.0,
        gt=0.0,
        description="Exponential-decay half life for baseline updates",
    )

    @property
    def is_calibrated(self) -> bool:
        """Check if user has been calibrated."""
        return self.calibrated_at is not None


class StateTransition(BaseModel):
    """Record of a state transition event."""

    timestamp: float = Field(
        ...,
        deprecated=True,
        description=(
            "Deprecated v1 monotonic seconds in the producing boot."
        ),
    )
    schema_version: Literal["2.0"] = "2.0"
    observed_at_unix_ms: int | None = Field(None, ge=0)
    observed_at_mono_ns: int | None = Field(None, ge=0)
    boot_id: UUID | None = None
    from_state: Literal["UNKNOWN", "FLOW", "HYPO", "HYPER", "RECOVERY"] = Field(
        ..., description="Previous state"
    )
    to_state: Literal["UNKNOWN", "FLOW", "HYPO", "HYPER", "RECOVERY"] = Field(
        ..., description="New state"
    )
    from_confidence: float = Field(
        ..., ge=0.0, le=1.0, description="Confidence before transition"
    )
    to_confidence: float = Field(
        ..., ge=0.0, le=1.0, description="Confidence after transition"
    )
    dwell_seconds: float = Field(
        ..., ge=0.0, description="Time spent in previous state"
    )
    trigger_reasons: list[str] = Field(
        default_factory=list, description="Reasons for transition"
    )

    @model_validator(mode="after")
    def _validate_v2_time_tuple(self) -> StateTransition:
        supplied = (
            self.observed_at_unix_ms is not None,
            self.observed_at_mono_ns is not None,
            self.boot_id is not None,
        )
        if any(supplied) and not all(supplied):
            raise ValueError("state transition v2 time fields must be supplied together")
        return self

    @property
    def is_escalation(self) -> bool:
        """Check if this is an escalation to overwhelm."""
        return self.to_state == "HYPER" and self.from_state != "HYPER"

    @property
    def is_recovery(self) -> bool:
        """Check if this is a recovery from overwhelm."""
        return self.from_state == "HYPER" and self.to_state in ("FLOW", "RECOVERY")
