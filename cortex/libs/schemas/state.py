"""
Cortex State Schemas

Pydantic models for user state estimation, baselines, and state transitions.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class UserState(str, Enum):
    """User cognitive state classification."""

    FLOW = "FLOW"  # Optimal engagement
    HYPO = "HYPO"  # Under-arousal, disengagement
    HYPER = "HYPER"  # Over-arousal, overwhelm
    RECOVERY = "RECOVERY"  # Transitioning back to flow
    HYPO_APNEA = "HYPO_APNEA"


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

    @property
    def overall(self) -> float:
        """Compute overall signal quality as weighted average."""
        weights = [0.4, 0.3, 0.3]  # Physio weighted higher
        qualities = [self.physio, self.kinematics, self.telemetry]
        return sum(w * q for w, q in zip(weights, qualities))

    @property
    def acceptable(self) -> bool:
        """Check if signal quality is acceptable for intervention."""
        return self.overall >= 0.3


class StateScores(BaseModel):
    """Scores for each possible user state."""

    flow: float = Field(0.0, ge=0.0, le=1.0, description="Flow state score")
    hypo: float = Field(0.0, ge=0.0, le=1.0, description="Hypo-arousal score")
    hyper: float = Field(0.0, ge=0.0, le=1.0, description="Hyper-arousal score")
    recovery: float = Field(0.0, ge=0.0, le=1.0, description="Recovery state score")

    def dominant_state(self) -> tuple[UserState, float]:
        """Get the dominant state and its score."""
        scores = {
            UserState.FLOW: self.flow,
            UserState.HYPO: self.hypo,
            UserState.HYPER: self.hyper,
            UserState.RECOVERY: self.recovery,
        }
        dominant = max(scores, key=lambda k: scores[k])
        return dominant, scores[dominant]


class StateEstimate(BaseModel):
    """
    Complete state estimation output from the state engine.

    Produced every 500ms from fused feature vectors.
    """

    state: Literal["FLOW", "HYPO", "HYPER", "RECOVERY"] = Field(
        ..., description="Classified user state"
    )
    confidence: float = Field(
        ..., ge=0.0, le=1.0, description="Confidence in state classification"
    )
    scores: StateScores = Field(
        ..., description="Raw scores for each state"
    )
    reasons: list[str] = Field(
        default_factory=list,
        description="Human-readable reasons for current state",
    )
    signal_quality: SignalQuality = Field(
        ..., description="Signal quality per channel"
    )
    timestamp: float = Field(..., description="Monotonic timestamp")
    dwell_seconds: float = Field(
        0.0, ge=0.0, description="Seconds in current state"
    )
    stress_integral: float | None = Field(
        None, ge=0.0, description="Cumulative stress load integral (ms*s)"
    )

    @property
    def is_overwhelmed(self) -> bool:
        """Check if user is in overwhelmed (HYPER) state."""
        return self.state == "HYPER"

    @property
    def is_flow(self) -> bool:
        """Check if user is in flow state."""
        return self.state == "FLOW"

    @property
    def should_intervene(self) -> bool:
        """
        Quick check if intervention conditions might be met.

        Full trigger policy check happens in intervention engine.
        """
        return (
            self.is_overwhelmed
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
        500.0, ge=100.0, le=2000.0, description="Baseline mouse velocity (px/s)"
    )
    mouse_variance_baseline: float = Field(
        10000.0, ge=1000.0, le=100000.0, description="Baseline mouse variance"
    )
    shoulder_neutral_y: float = Field(
        0.5, ge=0.0, le=1.0, description="Neutral shoulder Y position (normalized)"
    )
    resp_baseline: float = Field(
        15.0, ge=4.0, le=30.0, description="Baseline respiration rate (breaths/min)"
    )
    calibrated_at: datetime | None = Field(
        None, description="When calibration was performed"
    )

    @property
    def is_calibrated(self) -> bool:
        """Check if user has been calibrated."""
        return self.calibrated_at is not None


class StateTransition(BaseModel):
    """Record of a state transition event."""

    timestamp: float = Field(..., description="When transition occurred")
    from_state: Literal["FLOW", "HYPO", "HYPER", "RECOVERY"] = Field(
        ..., description="Previous state"
    )
    to_state: Literal["FLOW", "HYPO", "HYPER", "RECOVERY"] = Field(
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

    @property
    def is_escalation(self) -> bool:
        """Check if this is an escalation to overwhelm."""
        return self.to_state == "HYPER" and self.from_state != "HYPER"

    @property
    def is_recovery(self) -> bool:
        """Check if this is a recovery from overwhelm."""
        return self.from_state == "HYPER" and self.to_state in ("FLOW", "RECOVERY")
