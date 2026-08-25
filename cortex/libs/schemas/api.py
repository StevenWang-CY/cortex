"""Canonical REST request and response contracts.

Every model exposed through FastAPI lives under ``libs.schemas`` so the same
Pydantic definitions generate the browser and VS Code TypeScript surfaces.
Routes contain transport behavior only; they do not own duplicate wire types.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from cortex.libs.schemas.context import TaskContext
from cortex.libs.schemas.features import FeatureVector
from cortex.libs.schemas.intervention import (
    InterventionApplyResult,
    InterventionOutcome,
    InterventionPlan,
    WorkspaceSnapshot,
)
from cortex.libs.schemas.state import (
    EstimateStatus,
    InferenceModelIdentity,
    SignalQuality,
    StateEstimate,
    SupportState,
)
from cortex.libs.schemas.temporal import DualClockModel


class AckResponse(DualClockModel):
    status: str = "ok"


class ShutdownResponse(DualClockModel):
    status: str = "shutting_down"


class DashboardRaiseRequest(BaseModel):
    target: str | None = None


class DashboardRaiseResponse(DualClockModel):
    raised: bool = True
    target: str | None = None


class HealthResponse(DualClockModel):
    status: str
    services: dict[str, str]
    uptime_seconds: float
    version: str | None = None
    duplicate_intervention_acks: int = 0
    frames_dropped_total: int = 0
    store_degraded: bool = False
    feedback_log_read_failures: int = 0


class StatusResponse(DualClockModel):
    status: Literal["initializing", "ready", "degraded"] = "initializing"
    state: str | None = None
    support_state: SupportState | None = None
    estimate_status: EstimateStatus | None = None
    confidence: float | None = None
    evidence_strength: float | None = None
    evidence_coverage: float | None = None
    model: InferenceModelIdentity | None = None
    signal_quality: SignalQuality | None = None
    features: FeatureVector | None = None


class StateInferRequest(BaseModel):
    feature_vector: FeatureVector
    signal_quality: SignalQuality


class StateInferResponse(DualClockModel):
    estimate: StateEstimate
    source: Literal["rules", "classifier", "fallback"] = "rules"
    degraded: bool = False


class ContextBuildRequest(BaseModel):
    include_editor: bool = True
    include_terminal: bool = True
    include_browser: bool = True


class ContextBuildResponse(DualClockModel):
    context: TaskContext | None = None
    available: bool = False


class LLMPlanRequest(BaseModel):
    state_estimate: StateEstimate
    task_context: TaskContext


class LLMPlanResponse(DualClockModel):
    plan: InterventionPlan | None = None
    fallback_used: bool = False


class InterventionApplyRequest(BaseModel):
    plan: InterventionPlan


class InterventionApplyResponse(DualClockModel):
    applied: bool = False
    snapshot: WorkspaceSnapshot | None = None
    correlation_id: str | None = None
    confirmation: InterventionApplyResult | None = None


class InterventionRestoreRequest(BaseModel):
    intervention_id: str
    user_action: str = "dismissed"


class InterventionRestoreResponse(DualClockModel):
    restored: bool = False
    outcome: InterventionOutcome | None = None


class StressIntegralResponse(DualClockModel):
    status: Literal["unavailable"] = "unavailable"
    unavailable_reason: Literal["validation_required"] = "validation_required"
    current_value: float = 0.0
    threshold: float = 0.0
    should_break: bool = False
    sensitivity_multiplier: float = 1.0


class HelpfulnessSummaryResponse(DualClockModel):
    total_interventions: int = 0
    total_tracked: int = 0
    mean_reward: float = 0.0
    engagement_rate: float = 0.0
    positive_rate: float = 0.0
    recent_rewards: list[float] = Field(default_factory=list)


class ConsentLevelResponse(DualClockModel):
    levels: dict[str, dict[str, Any]] = Field(default_factory=dict)


class ConsentResetRequest(BaseModel):
    """Reserved body for a future consent reset reason/actor."""


class ConsentResetResponse(DualClockModel):
    reset: bool = False
    levels: dict[str, dict[str, Any]] = Field(default_factory=dict)


class ProjectListResponse(DualClockModel):
    projects: list[dict[str, Any]] = Field(default_factory=list)


class LaunchProjectResponse(DualClockModel):
    launched: bool = False
    project_name: str = ""
    errors: list[str] = Field(default_factory=list)


class FeedbackRequest(BaseModel):
    description: str = Field(..., min_length=10, max_length=500)
    include_logs: bool = False
    app_version: str = Field("", max_length=64)
    user_agent: str = Field("", max_length=512)


class FeedbackResponse(DualClockModel):
    ok: bool = True
    report_id: str = ""


__all__ = [name for name in globals() if name.endswith(("Request", "Response"))]
