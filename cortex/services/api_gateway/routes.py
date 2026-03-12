"""
API Gateway — REST Routes

All REST endpoints for the Cortex internal service API:

Capture & Features:
  POST /capture/frame_meta    — Submit frame metadata
  POST /features/physio       — Submit physio features
  POST /features/kinematics   — Submit kinematic features
  POST /features/telemetry    — Submit telemetry features

State & Context:
  POST /state/infer           — Compute state from fused features
  POST /context/build         — Build task context from adapters

LLM & Intervention:
  POST /llm/plan              — Request intervention plan
  POST /intervention/apply    — Apply intervention to workspace
  POST /intervention/restore  — Restore workspace to pre-intervention state

Status & Health:
  GET  /status/current        — Current system state, confidence, signal quality
  GET  /health                — Health check for all services
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from cortex.libs.schemas.context import TaskContext
from cortex.libs.schemas.features import (
    FeatureVector,
    FrameMeta,
    KinematicFeatures,
    PhysioFeatures,
    TelemetryFeatures,
)
from cortex.libs.schemas.intervention import (
    InterventionOutcome,
    InterventionPlan,
    WorkspaceSnapshot,
)
from cortex.libs.schemas.state import SignalQuality, StateEstimate, StateScores
from cortex.services.intervention_engine import capture_snapshot, prepare_plan

logger = logging.getLogger(__name__)

router = APIRouter()


# =============================================================================
# Response models
# =============================================================================


class AckResponse(BaseModel):
    """Simple acknowledgement response."""

    status: str = "ok"
    timestamp: float = Field(default_factory=time.monotonic)


class HealthResponse(BaseModel):
    """Health check response."""

    status: str
    services: dict[str, str]
    uptime_seconds: float


class StatusResponse(BaseModel):
    """Current system status."""

    state: str | None = None
    confidence: float | None = None
    signal_quality: SignalQuality | None = None
    features: FeatureVector | None = None
    timestamp: float = Field(default_factory=time.monotonic)


class StateInferRequest(BaseModel):
    """Request to infer state from a feature vector."""

    feature_vector: FeatureVector
    signal_quality: SignalQuality


class StateInferResponse(BaseModel):
    """State inference result."""

    estimate: StateEstimate
    timestamp: float = Field(default_factory=time.monotonic)


class ContextBuildRequest(BaseModel):
    """Request to build task context."""

    include_editor: bool = True
    include_terminal: bool = True
    include_browser: bool = True


class ContextBuildResponse(BaseModel):
    """Context build result."""

    context: TaskContext | None = None
    available: bool = False
    timestamp: float = Field(default_factory=time.monotonic)


class LLMPlanRequest(BaseModel):
    """Request intervention plan from LLM."""

    state_estimate: StateEstimate
    task_context: TaskContext


class LLMPlanResponse(BaseModel):
    """LLM plan result."""

    plan: InterventionPlan | None = None
    fallback_used: bool = False
    timestamp: float = Field(default_factory=time.monotonic)


class InterventionApplyRequest(BaseModel):
    """Request to apply an intervention."""

    plan: InterventionPlan


class InterventionApplyResponse(BaseModel):
    """Intervention apply result."""

    applied: bool = False
    snapshot: WorkspaceSnapshot | None = None
    timestamp: float = Field(default_factory=time.monotonic)


class InterventionRestoreRequest(BaseModel):
    """Request to restore workspace from snapshot."""

    intervention_id: str
    user_action: str = "dismissed"


class InterventionRestoreResponse(BaseModel):
    """Intervention restore result."""

    restored: bool = False
    outcome: InterventionOutcome | None = None
    timestamp: float = Field(default_factory=time.monotonic)


# Track app start time for uptime computation
_start_time: float = time.monotonic()


def _get_registry(request: Request) -> Any:
    """Get the service registry from app state."""
    return request.app.state.registry


def _get_first_service(registry: Any, *names: str) -> Any | None:
    """Return the first registered service that exists."""
    for name in names:
        service = registry.get(name)
        if service is not None:
            return service
    return None


async def _build_snapshot_for_plan(registry: Any, plan: InterventionPlan) -> WorkspaceSnapshot:
    """Build the best available workspace snapshot for an intervention."""
    context = registry.get("latest_task_context")
    if context is None:
        context_engine = registry.get("context_engine")
        if context_engine is not None and hasattr(context_engine, "build_context"):
            try:
                context = await context_engine.build_context()
            except Exception:
                logger.exception("Failed to build context while snapshotting intervention")
    snapshot = capture_snapshot(context, intervention_id=plan.intervention_id)
    registry.register(f"workspace_snapshot:{plan.intervention_id}", snapshot)
    return snapshot


# =============================================================================
# Health & Status
# =============================================================================


@router.get("/health", response_model=HealthResponse)
async def health_check(request: Request) -> HealthResponse:
    """Health check for all services."""
    reg = _get_registry(request)
    services: dict[str, str] = {}

    for name in reg.registered_services:
        svc = reg.get(name)
        if svc is not None:
            services[name] = "up"
        else:
            services[name] = "unknown"

    overall = "healthy" if reg.healthy else "unhealthy"

    return HealthResponse(
        status=overall,
        services=services,
        uptime_seconds=time.monotonic() - _start_time,
    )


@router.get("/status/current", response_model=StatusResponse)
async def get_current_status(request: Request) -> StatusResponse:
    """Get current system state, confidence, and signal quality."""
    reg = _get_registry(request)

    # Try to get state from state engine
    state_engine = reg.get("state_engine")
    if state_engine is not None and hasattr(state_engine, "latest_estimate"):
        est = state_engine.latest_estimate
        if est is not None:
            return StatusResponse(
                state=est.state,
                confidence=est.confidence,
                signal_quality=est.signal_quality,
                timestamp=est.timestamp,
            )

    # Try to get from stored latest
    latest_state = reg.get("latest_state_estimate")
    if latest_state is not None:
        return StatusResponse(
            state=latest_state.state,
            confidence=latest_state.confidence,
            signal_quality=latest_state.signal_quality,
            timestamp=latest_state.timestamp,
        )

    return StatusResponse()


# =============================================================================
# Capture & Features
# =============================================================================


@router.post("/capture/frame_meta", response_model=AckResponse)
async def submit_frame_meta(
    frame_meta: FrameMeta, request: Request,
) -> AckResponse:
    """Submit frame metadata from capture service."""
    reg = _get_registry(request)

    # Store latest frame meta
    reg.register("latest_frame_meta", frame_meta)

    # Forward to any subscribed services
    capture_handler = reg.get("capture_handler")
    if capture_handler is not None and callable(capture_handler):
        await capture_handler(frame_meta)

    return AckResponse()


@router.post("/features/physio", response_model=AckResponse)
async def submit_physio_features(
    features: PhysioFeatures, request: Request,
) -> AckResponse:
    """Submit physio features from physio engine."""
    reg = _get_registry(request)

    reg.register("latest_physio", features)

    # Forward to feature fusion if available
    fusion = reg.get("feature_fusion")
    if fusion is not None and hasattr(fusion, "update_physio"):
        fusion.update_physio(features)

    return AckResponse()


@router.post("/features/kinematics", response_model=AckResponse)
async def submit_kinematic_features(
    features: KinematicFeatures, request: Request,
) -> AckResponse:
    """Submit kinematic features from kinematics engine."""
    reg = _get_registry(request)

    reg.register("latest_kinematics", features)

    fusion = reg.get("feature_fusion")
    if fusion is not None and hasattr(fusion, "update_kinematics"):
        fusion.update_kinematics(features)

    return AckResponse()


@router.post("/features/telemetry", response_model=AckResponse)
async def submit_telemetry_features(
    features: TelemetryFeatures, request: Request,
) -> AckResponse:
    """Submit telemetry features from telemetry engine."""
    reg = _get_registry(request)

    reg.register("latest_telemetry", features)

    fusion = reg.get("feature_fusion")
    if fusion is not None and hasattr(fusion, "update_telemetry"):
        fusion.update_telemetry(features)

    return AckResponse()


# =============================================================================
# State Inference
# =============================================================================


@router.post("/state/infer", response_model=StateInferResponse)
async def infer_state(
    body: StateInferRequest, request: Request,
) -> StateInferResponse:
    """Compute state from fused features."""
    reg = _get_registry(request)

    # Try to use registered scorer + smoother
    scorer = reg.get("rule_scorer")
    smoother = reg.get("score_smoother")

    if scorer is not None and smoother is not None:
        scores = scorer.compute_scores(body.feature_vector)
        estimate = smoother.update(scores, body.signal_quality)
        reg.register("latest_state_estimate", estimate)
        return StateInferResponse(estimate=estimate)

    # Fallback: produce a basic estimate without engines
    estimate = StateEstimate(
        state="FLOW",
        confidence=0.5,
        scores=StateScores(flow=0.5, hypo=0.0, hyper=0.0, recovery=0.0),
        reasons=["No state engine registered, using default"],
        signal_quality=body.signal_quality,
        timestamp=body.feature_vector.timestamp,
        dwell_seconds=0.0,
    )
    reg.register("latest_state_estimate", estimate)
    return StateInferResponse(estimate=estimate)


# =============================================================================
# Context Building
# =============================================================================


@router.post("/context/build", response_model=ContextBuildResponse)
async def build_context(
    body: ContextBuildRequest, request: Request,
) -> ContextBuildResponse:
    """Build task context from workspace adapters."""
    reg = _get_registry(request)

    context_engine = reg.get("context_engine")
    if context_engine is not None and hasattr(context_engine, "build_context"):
        ctx = await context_engine.build_context(
            include_editor=body.include_editor,
            include_terminal=body.include_terminal,
            include_browser=body.include_browser,
        )
        return ContextBuildResponse(context=ctx, available=True)

    return ContextBuildResponse(available=False)


# =============================================================================
# LLM Planning
# =============================================================================


@router.post("/llm/plan", response_model=LLMPlanResponse)
async def request_llm_plan(
    body: LLMPlanRequest, request: Request,
) -> LLMPlanResponse:
    """Request intervention plan from LLM engine."""
    reg = _get_registry(request)

    llm_engine = reg.get("llm_engine")
    if llm_engine is not None:
        if hasattr(llm_engine, "generate_intervention_plan"):
            plan = await llm_engine.generate_intervention_plan(
                body.task_context,
                body.state_estimate,
            )
            return LLMPlanResponse(plan=plan)
        if hasattr(llm_engine, "generate_plan"):
            plan = await llm_engine.generate_plan(
                body.state_estimate, body.task_context,
            )
            return LLMPlanResponse(plan=plan)

    llm_client = _get_first_service(reg, "llm_client", "remote_qwen_client", "local_ollama_client")
    if llm_client is not None and hasattr(llm_client, "generate_intervention_plan"):
        plan = await llm_client.generate_intervention_plan(
            body.task_context,
            body.state_estimate,
        )
        return LLMPlanResponse(plan=plan)

    return LLMPlanResponse(fallback_used=True)


# =============================================================================
# Intervention
# =============================================================================


@router.post("/intervention/apply", response_model=InterventionApplyResponse)
async def apply_intervention(
    body: InterventionApplyRequest, request: Request,
) -> InterventionApplyResponse:
    """Apply intervention to workspace."""
    reg = _get_registry(request)

    intervention_engine = reg.get("intervention_engine")
    if intervention_engine is not None and hasattr(intervention_engine, "apply"):
        snapshot = await intervention_engine.apply(body.plan)
        return InterventionApplyResponse(applied=True, snapshot=snapshot)

    executor = _get_first_service(reg, "intervention_executor", "executor")
    if executor is not None and hasattr(executor, "apply"):
        validation, commands = prepare_plan(body.plan)
        if not validation.is_valid:
            logger.warning(
                "Rejected intervention plan %s: %s",
                body.plan.intervention_id,
                validation.errors,
            )
            return InterventionApplyResponse(applied=False)

        snapshot = await _build_snapshot_for_plan(reg, body.plan)
        mutations = await executor.apply(body.plan, commands)
        applied = bool(mutations) and all(m.success for m in mutations)

        restore_manager = _get_first_service(reg, "restore_manager", "intervention_restore_manager")
        if restore_manager is not None and hasattr(restore_manager, "start_intervention"):
            restore_manager.start_intervention(
                body.plan.intervention_id,
                snapshot,
            )

        return InterventionApplyResponse(applied=applied, snapshot=snapshot)

    return InterventionApplyResponse(applied=False)


@router.post("/intervention/restore", response_model=InterventionRestoreResponse)
async def restore_intervention(
    body: InterventionRestoreRequest, request: Request,
) -> InterventionRestoreResponse:
    """Restore workspace to pre-intervention state."""
    reg = _get_registry(request)

    intervention_engine = reg.get("intervention_engine")
    if intervention_engine is not None and hasattr(intervention_engine, "restore"):
        outcome = await intervention_engine.restore(
            body.intervention_id, body.user_action,
        )
        return InterventionRestoreResponse(restored=True, outcome=outcome)

    restore_manager = _get_first_service(reg, "restore_manager", "intervention_restore_manager")
    if restore_manager is not None:
        if body.user_action == "engaged" and hasattr(restore_manager, "engage"):
            outcome = await restore_manager.engage(body.intervention_id)
        elif hasattr(restore_manager, "dismiss"):
            outcome = await restore_manager.dismiss(body.intervention_id)
        else:
            outcome = None

        if outcome is not None:
            return InterventionRestoreResponse(
                restored=outcome.workspace_restored,
                outcome=outcome,
            )

    return InterventionRestoreResponse(restored=False)
