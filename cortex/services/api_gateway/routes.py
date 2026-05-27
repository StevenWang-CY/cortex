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
from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from cortex.libs.config.ports import HTTP_API_PORT, WEBSOCKET_PORT
from cortex.libs.logging.correlation import get_correlation_id
from cortex.libs.logging.structured import EventType
from cortex.libs.ports.intervention_port import InterventionPort
from cortex.libs.schemas.context import TaskContext
from cortex.libs.schemas.features import (
    FeatureVector,
    FrameMeta,
    KinematicFeatures,
    PhysioFeatures,
    TelemetryFeatures,
)
from cortex.libs.schemas.intervention import (
    InterventionApplyResult,
    InterventionOutcome,
    InterventionPlan,
    WorkspaceSnapshot,
)
from cortex.libs.schemas.session_history import (
    SessionDetailResponse,
    SessionListResponse,
    TrendsResponse,
)
from cortex.libs.schemas.state import SignalQuality, StateEstimate, StateScores
from cortex.libs.schemas.ws_message_types import MessageType
from cortex.services.intervention_engine import (
    capture_snapshot as _engine_capture_snapshot,
)
from cortex.services.intervention_engine import (
    prepare_plan as _engine_prepare_plan,
)


def _get_intervention_port(request: Request) -> InterventionPort | None:
    """Phase-4b TASK K: resolve the configured ``InterventionPort`` from
    ``app.state.intervention_port`` (Phase-4b TASK L wires it on
    startup). Falls back to the module-level engine functions for
    legacy test rigs that construct the app without binding the port
    explicitly so we don't break existing fixtures.
    """
    port = getattr(getattr(request, "app", None), "state", None)
    if port is not None:
        return getattr(port, "intervention_port", None)
    return None


def capture_snapshot(
    context: TaskContext | None = None,
    intervention_id: str | None = None,
    *,
    request: Request | None = None,
    timestamp: float | None = None,
) -> WorkspaceSnapshot:
    """Phase-4b TASK K: thin shim that prefers the
    ``app.state.intervention_port`` capability when available and
    falls back to the engine module-level function otherwise. Keeps
    every existing call site working without rewriting signatures."""
    port = _get_intervention_port(request) if request is not None else None
    if port is not None:
        return port.capture_snapshot(
            context, intervention_id, timestamp=timestamp,
        )
    return _engine_capture_snapshot(
        context, intervention_id=intervention_id, timestamp=timestamp,
    )


def prepare_plan(
    plan: InterventionPlan,
    *,
    tab_count: int | None = None,
    request: Request | None = None,
) -> Any:
    """Phase-4b TASK K: prefer the injected port; fall back to the
    engine module-level function for legacy rigs."""
    port = _get_intervention_port(request) if request is not None else None
    if port is not None:
        return port.prepare_plan(plan, tab_count=tab_count)
    return _engine_prepare_plan(plan, tab_count=tab_count)

logger = logging.getLogger(__name__)

# Two routers — a public liveness-only router and an authenticated
# router that owns every mutating endpoint. ``app.py`` mounts each with
# the appropriate dependency. The split is structural: defining a new
# mutating endpoint on ``health_router`` is visible in code review;
# defining it on ``router`` automatically inherits the systemic auth
# gate via the ``include_router(dependencies=[…])`` wiring. See audit
# Debt-2 closure in ``audit/execution-log.md``.
router = APIRouter()
health_router = APIRouter()


# =============================================================================
# Response models
# =============================================================================


class AckResponse(BaseModel):
    """Simple acknowledgement response."""

    status: str = "ok"
    # Phase-4a fix: use wall-clock seconds so clients can compare this to
    # ``Date.now() / 1000`` without a monotonic-vs-epoch unit mismatch.
    timestamp: float = Field(default_factory=time.time)


class ShutdownResponse(BaseModel):
    """Response for the /shutdown endpoint."""

    status: str = "shutting_down"
    # Phase-4a fix: see ``AckResponse.timestamp``.
    timestamp: float = Field(default_factory=time.time)


@router.post("/shutdown", response_model=ShutdownResponse)
async def shutdown(request: Request) -> ShutdownResponse:
    """Gracefully shut down the Cortex daemon.

    Phase-4b TASK K: the daemon HTTP API listens on
    ``HTTP_API_PORT`` (default
    :data:`cortex.libs.config.ports.HTTP_API_PORT`); the paired WS
    server lives on ``WEBSOCKET_PORT``
    (:data:`cortex.libs.config.ports.WEBSOCKET_PORT`).
    """
    import asyncio
    import os
    import signal as _signal
    logger.info(
        f"Shutdown requested via API (port={HTTP_API_PORT})",
    )
    # Schedule shutdown after response is sent
    loop = asyncio.get_running_loop()
    loop.call_later(0.5, os.kill, os.getpid(), _signal.SIGTERM)
    return ShutdownResponse(status="shutting_down")


class DashboardRaiseRequest(BaseModel):
    """Phase-4b TASK K: optional ``target`` hint for the dashboard."""

    target: str | None = None


class DashboardRaiseResponse(BaseModel):
    """Phase-4b TASK K: result of a /dashboard/raise call."""

    raised: bool = True
    target: str | None = None
    timestamp: float = Field(
        default_factory=time.time,
        description="Wall-clock seconds since epoch (UTC).",
    )


@router.post("/dashboard/raise", response_model=DashboardRaiseResponse)
async def raise_dashboard(
    body: DashboardRaiseRequest | None,
    request: Request,
) -> DashboardRaiseResponse:
    """Phase-4b TASK K: instruct the desktop shell to raise its window.

    Emits :attr:`MessageType.RAISE_DASHBOARD` over the WS bus. The
    desktop shell handles the message; the route returns ``raised``
    optimistically because the wire emission is fire-and-forget (the
    shell may not be running, in which case the request is silently
    dropped by every receiver).
    """
    target = body.target if body is not None else None
    reg = _get_registry(request)
    ws_server = reg.get("ws_server")
    if ws_server is not None and hasattr(ws_server, "send_message"):
        try:
            await ws_server.send_message(
                MessageType.RAISE_DASHBOARD.value,
                {"target": target},
                target_client_types=["desktop"],
            )
        except Exception:
            logger.exception(
                "RAISE_DASHBOARD broadcast failed (ws_port=%d)",
                WEBSOCKET_PORT,
            )
            return DashboardRaiseResponse(raised=False, target=target)
    return DashboardRaiseResponse(raised=True, target=target)


class HealthResponse(BaseModel):
    """Health check response."""

    status: str
    services: dict[str, str]
    uptime_seconds: float
    # G2 (audit-prod): expose the daemon version so the browser
    # extension's CONNECTIVITY_DIAGNOSTIC can detect a version mismatch
    # between the installed extension and the running daemon.
    version: str | None = None
    # B2 (Phase 4.1): operator-facing counter of duplicate
    # INTERVENTION_APPLIED ack frames seen since the daemon started.
    # A nonzero value points at extension misbehaviour (re-acking the
    # same phase) but is non-fatal — the dedup logic still suppresses
    # the second mutation overwrite.
    duplicate_intervention_acks: int = 0
    # B3 (Phase 4.1): operator-facing counter of frames the capture
    # pipeline evicted from its output queue when the queue was full.
    # A sustained nonzero value means a downstream consumer is slower
    # than the capture rate; rPPG / kinematics quality may degrade.
    frames_dropped_total: int = 0
    # B4 (Phase 4.1): True when the daemon fell back to the in-memory
    # store at boot because Redis was unreachable. Persistence is
    # non-durable while this is True.
    store_degraded: bool = False
    # B5 (Phase 4.1): count of feedback bundle log-tail reads that
    # raised OSError. A spike here usually means the daemon's log path
    # was rotated out from under us.
    feedback_log_read_failures: int = 0


class StatusResponse(BaseModel):
    """Current system status.

    Phase 4.4 T3: ``status`` is an explicit discriminator that clients can
    use without inspecting the nullability of ``state``/``features``. The
    legacy optional fields are preserved so callers that already inspect
    them continue to compile; new clients should branch on ``status``.
    """

    status: Literal["initializing", "ready", "degraded"] = Field(
        "initializing",
        description=(
            "Coarse readiness discriminator. ``initializing`` = no "
            "estimate yet; ``ready`` = state engine producing live "
            "estimates; ``degraded`` = serving a synthetic/last-known "
            "estimate because real inference failed."
        ),
    )
    state: str | None = None
    confidence: float | None = None
    signal_quality: SignalQuality | None = None
    features: FeatureVector | None = None
    timestamp: float = Field(
        default_factory=time.time,
        description="Wall-clock seconds since epoch (UTC).",
    )


class StateInferRequest(BaseModel):
    """Request to infer state from a feature vector."""

    feature_vector: FeatureVector
    signal_quality: SignalQuality


class StateInferResponse(BaseModel):
    """State inference result.

    F18 (audit): the ``source`` and ``degraded`` envelope fields let the
    UI distinguish a real classifier confidence from a synthetic
    fallback. Pre-fix, a 0.5 confidence from the fallback path looked
    identical to a 0.5 confidence from the rule scorer — observability
    and correctness in one bug. Defaults are chosen so existing callers
    that don't read the fields still get the same wire shape they had
    before plus two extra boolean-ish fields they can safely ignore.
    """

    estimate: StateEstimate
    timestamp: float = Field(
        default_factory=time.time,
        description="Wall-clock seconds since epoch (UTC).",
    )
    source: Literal["classifier", "fallback"] = Field(
        "classifier",
        description=(
            "``classifier`` when the rule scorer + smoother produced the "
            "estimate; ``fallback`` when those engines were missing or "
            "raised and the route returned a synthetic estimate."
        ),
    )
    degraded: bool = Field(
        False,
        description=(
            "True when the daemon could not run real inference and is "
            "serving a synthetic estimate. UIs surface a banner so the "
            "user understands the state stream is not authoritative."
        ),
    )


class ContextBuildRequest(BaseModel):
    """Request to build task context."""

    include_editor: bool = True
    include_terminal: bool = True
    include_browser: bool = True


class ContextBuildResponse(BaseModel):
    """Context build result."""

    context: TaskContext | None = None
    available: bool = False
    timestamp: float = Field(
        default_factory=time.time,
        description="Wall-clock seconds since epoch (UTC).",
    )


class LLMPlanRequest(BaseModel):
    """Request intervention plan from LLM."""

    state_estimate: StateEstimate
    task_context: TaskContext


class LLMPlanResponse(BaseModel):
    """LLM plan result."""

    plan: InterventionPlan | None = None
    fallback_used: bool = False
    timestamp: float = Field(
        default_factory=time.time,
        description="Wall-clock seconds since epoch (UTC).",
    )


class InterventionApplyRequest(BaseModel):
    """Request to apply an intervention."""

    plan: InterventionPlan


class InterventionApplyResponse(BaseModel):
    """Intervention apply result.

    F05: ``applied`` mirrors the optimistic adapter's pre-F05 contract
    (mutations dispatched successfully). ``confirmation`` is the real
    ack-driven outcome surfaced when ``await_confirmation`` is honoured;
    callers that pass ``await_confirmation=False`` receive a 202-style
    response with ``correlation_id`` populated so they can poll later.
    """

    applied: bool = False
    snapshot: WorkspaceSnapshot | None = None
    correlation_id: str | None = None
    confirmation: InterventionApplyResult | None = None
    timestamp: float = Field(
        default_factory=time.time,
        description="Wall-clock seconds since epoch (UTC).",
    )


class InterventionRestoreRequest(BaseModel):
    """Request to restore workspace from snapshot."""

    intervention_id: str
    user_action: str = "dismissed"


class InterventionRestoreResponse(BaseModel):
    """Intervention restore result."""

    restored: bool = False
    outcome: InterventionOutcome | None = None
    timestamp: float = Field(
        default_factory=time.time,
        description="Wall-clock seconds since epoch (UTC).",
    )


# Track app start time for uptime computation
_start_time: float = time.monotonic()


# Audit-prod fix (P2-E): memoize the daemon version lookup so /health
# doesn't pay an ``importlib.metadata`` round-trip on every probe.
# Resolved exactly once at first /health call; ``None`` is a valid
# cached value (means: version not discoverable in this environment).
#
# Concurrency: writes to ``_DAEMON_VERSION_CACHE`` are not lock-guarded.
# Two concurrent first-callers may both compute the same value; this is
# tolerated because ``importlib.metadata.version`` is idempotent and the
# tuple replacement at the bottom of ``_resolve_daemon_version`` is a
# single bytecode store (atomic under CPython's GIL). The worst case is
# one extra resolution, never a torn value. We deliberately skip the
# Lock — /health is on the hot path and the lock cost would defeat the
# memoisation.
_DAEMON_VERSION_CACHE: tuple[bool, str | None] = (False, None)


def _resolve_daemon_version() -> str | None:
    global _DAEMON_VERSION_CACHE
    resolved, cached = _DAEMON_VERSION_CACHE
    if resolved:
        return cached
    version: str | None = None
    try:
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as _pkg_version

        try:
            version = _pkg_version("cortex")
        except PackageNotFoundError:
            try:
                from cortex import __version__ as _v

                version = _v
            except (ImportError, AttributeError):
                version = None
    except (ImportError, AttributeError):
        version = None
    _DAEMON_VERSION_CACHE = (True, version)
    return version


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


async def _build_snapshot_for_plan(
    registry: Any,
    plan: InterventionPlan,
    *,
    request: Request | None = None,
) -> WorkspaceSnapshot:
    """Build the best available workspace snapshot for an intervention."""
    context = registry.get("latest_task_context")
    if context is None:
        context_engine = registry.get("context_engine")
        if context_engine is not None and hasattr(context_engine, "build_context"):
            try:
                context = await context_engine.build_context()
            except Exception:
                logger.exception("Failed to build context while snapshotting intervention")
    snapshot = capture_snapshot(
        context, intervention_id=plan.intervention_id, request=request,
    )
    registry.register(f"workspace_snapshot:{plan.intervention_id}", snapshot)
    return snapshot


# =============================================================================
# Health & Status
# =============================================================================


@health_router.get("/health", response_model=HealthResponse)
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

    # B2/B3/B4/B5 (Phase 4.1): surface the operator-facing diagnostic
    # counters when a daemon instance is registered. Falls back to
    # zeros for legacy test rigs that build the FastAPI app without a
    # daemon (unit tests of the routes themselves).
    duplicate_acks = 0
    frames_dropped_total = 0
    store_degraded = False
    daemon = reg.get("daemon") if hasattr(reg, "get") else None
    if daemon is not None:
        duplicate_acks = int(
            getattr(daemon, "_duplicate_intervention_ack_count", 0) or 0
        )
        store_degraded = bool(getattr(daemon, "_store_degraded", False))
        pipeline = getattr(daemon, "_capture_pipeline", None)
        if pipeline is not None:
            frames_dropped_total = int(
                getattr(pipeline, "frames_dropped_total", 0) or 0
            )

    return HealthResponse(
        status=overall,
        services=services,
        uptime_seconds=time.monotonic() - _start_time,
        version=_resolve_daemon_version(),
        duplicate_intervention_acks=duplicate_acks,
        frames_dropped_total=frames_dropped_total,
        store_degraded=store_degraded,
        feedback_log_read_failures=int(_feedback_log_read_failures),
    )


@router.get("/status/current", response_model=StatusResponse)
async def get_current_status(request: Request) -> StatusResponse:
    """Get current system state, confidence, and signal quality.

    Phase 4.4 T3: stamps ``status`` so clients can branch without
    inspecting nullability of ``state``/``features``.
    """
    reg = _get_registry(request)

    # Try to get state from state engine
    state_engine = reg.get("state_engine")
    if state_engine is not None and hasattr(state_engine, "latest_estimate"):
        est = state_engine.latest_estimate
        if est is not None:
            return StatusResponse(
                status="ready",
                state=est.state,
                confidence=est.confidence,
                signal_quality=est.signal_quality,
                timestamp=est.timestamp,
            )

    # Try to get from stored latest
    latest_state = reg.get("latest_state_estimate")
    if latest_state is not None:
        return StatusResponse(
            status="ready",
            state=latest_state.state,
            confidence=latest_state.confidence,
            signal_quality=latest_state.signal_quality,
            timestamp=latest_state.timestamp,
        )

    return StatusResponse(status="initializing")


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
    """Compute state from fused features.

    F18 (audit): two paths now report distinct envelope shapes. The
    happy path stamps ``source="classifier"`` (the default); the
    fallback path stamps ``source="fallback"`` and ``degraded=True`` and
    emits :data:`EventType.STATE_INFER_DEGRADED` with the bound
    correlation id. A scorer/smoother exception is treated identically
    to the not-registered case — surfacing a synthetic confidence as if
    it were real is exactly the failure mode the audit flagged.
    """
    reg = _get_registry(request)

    # Try to use registered scorer + smoother
    scorer = reg.get("rule_scorer")
    smoother = reg.get("score_smoother")

    if scorer is not None and smoother is not None:
        try:
            scores = scorer.compute_scores(body.feature_vector)
            estimate = smoother.update(scores, body.signal_quality)
        except Exception:
            # F18: scorer/smoother raised — fall through to the synthetic
            # estimate but flag the response as degraded so the UI can
            # show a banner instead of silently believing a 0.5
            # confidence is authoritative.
            logger.exception("rule scorer / smoother raised; serving fallback estimate")
        else:
            reg.register("latest_state_estimate", estimate)
            return StateInferResponse(estimate=estimate)

    # Fallback: produce a basic estimate without engines. Emit the
    # degradation telemetry so a log aggregator sees the failure even if
    # the response body is not inspected.
    logger.warning(
        "%s reason=%s cid=%s",
        EventType.STATE_INFER_DEGRADED.value,
        "scorer_or_smoother_missing" if (scorer is None or smoother is None) else "scorer_raised",
        get_correlation_id() or "-",
    )
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
    return StateInferResponse(
        estimate=estimate,
        source="fallback",
        degraded=True,
    )


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
            # B8 (Phase 4.1): tag the chosen planner branch so operators
            # can diff log distributions across deploys when one branch
            # silently degrades to the wrong fallback.
            logger.info(
                "LLM planner branch selected",
                extra={"planner_method": "llm_engine.generate_intervention_plan"},
            )
            plan = await llm_engine.generate_intervention_plan(
                body.task_context,
                body.state_estimate,
            )
            # B11 (Phase 4.1): inspect the discriminated failure_mode
            # and log a structured entry tagged with the result. Hands
            # downstream callers a stable signal to branch on (e.g.,
            # 'parse_error' triggers a retry hint to the operator).
            try:
                from cortex.services.llm_engine.anthropic_planner import (
                    classify_plan_failure_mode,
                )
                failure_mode = classify_plan_failure_mode(plan)
            except Exception:
                failure_mode = "ok"
            logger.info(
                "LLM planner result classified",
                extra={
                    "planner_method": "llm_engine.generate_intervention_plan",
                    "failure_mode": failure_mode,
                },
            )
            return LLMPlanResponse(plan=plan)
        if hasattr(llm_engine, "generate_plan"):
            logger.info(
                "LLM planner branch selected",
                extra={"planner_method": "llm_engine.generate_plan"},
            )
            plan = await llm_engine.generate_plan(
                body.state_estimate, body.task_context,
            )
            return LLMPlanResponse(plan=plan)

    # v0.2.1: only "llm_client" is registered — the legacy remote_qwen /
    # local_ollama service keys were removed as part of the Anthropic SDK
    # migration. Keep the call as a single-key lookup for symmetry with
    # the helper signature.
    llm_client = _get_first_service(reg, "llm_client")
    if llm_client is not None and hasattr(llm_client, "generate_intervention_plan"):
        logger.info(
            "LLM planner branch selected",
            extra={"planner_method": "llm_client.generate_intervention_plan"},
        )
        plan = await llm_client.generate_intervention_plan(
            body.task_context,
            body.state_estimate,
        )
        return LLMPlanResponse(plan=plan)

    logger.info(
        "LLM planner branch selected",
        extra={"planner_method": "fallback"},
    )
    return LLMPlanResponse(fallback_used=True)


# =============================================================================
# Intervention
# =============================================================================


@router.post("/intervention/apply", response_model=InterventionApplyResponse)
async def apply_intervention(
    body: InterventionApplyRequest,
    request: Request,
    await_confirmation: bool = True,
    confirmation_timeout_seconds: float = 30.0,
) -> InterventionApplyResponse:
    """Apply intervention to workspace.

    F05: when ``await_confirmation`` is True (the default), the call
    blocks until the extension's WS ``INTERVENTION_APPLIED`` ack lands
    or ``confirmation_timeout_seconds`` elapses. The response then
    surfaces the real per-action outcome via ``confirmation`` rather
    than the legacy always-optimistic ``applied=True``. Callers that
    want non-blocking semantics (the 202-style pattern in the audit
    plan) can pass ``await_confirmation=False`` and poll later using
    ``correlation_id``.
    """
    reg = _get_registry(request)
    correlation_id = (
        request.headers.get("X-Cortex-Request-ID")
        if request is not None
        else None
    )

    intervention_engine = reg.get("intervention_engine")
    if intervention_engine is not None and hasattr(intervention_engine, "apply"):
        snapshot = await intervention_engine.apply(body.plan)
        return InterventionApplyResponse(
            applied=True,
            snapshot=snapshot,
            correlation_id=correlation_id,
        )

    executor = _get_first_service(reg, "intervention_executor", "executor")
    if executor is not None and hasattr(executor, "apply"):
        validation, commands = prepare_plan(body.plan, request=request)
        if not validation.is_valid:
            logger.warning(
                "Rejected intervention plan %s: %s",
                body.plan.intervention_id,
                validation.errors,
            )
            return InterventionApplyResponse(
                applied=False, correlation_id=correlation_id,
            )

        snapshot = await _build_snapshot_for_plan(reg, body.plan, request=request)
        mutations = await executor.apply(body.plan, commands)
        applied = bool(mutations) and all(m.success for m in mutations)

        restore_manager = _get_first_service(reg, "restore_manager", "intervention_restore_manager")
        if restore_manager is not None and hasattr(restore_manager, "start_intervention"):
            restore_manager.start_intervention(
                body.plan.intervention_id,
                snapshot,
            )

        ws_server = reg.get("ws_server")
        if ws_server is not None and hasattr(ws_server, "send_intervention"):
            await ws_server.send_intervention(body.plan)

        confirmation = await _maybe_await_confirmation(
            reg,
            body.plan.intervention_id,
            correlation_id=correlation_id,
            await_confirmation=await_confirmation,
            timeout_seconds=confirmation_timeout_seconds,
        )
        return InterventionApplyResponse(
            applied=applied,
            snapshot=snapshot,
            correlation_id=correlation_id,
            confirmation=confirmation,
        )

    # No executor available — still broadcast to WS clients (Chrome overlay)
    ws_server = reg.get("ws_server")
    if ws_server is not None and hasattr(ws_server, "send_intervention"):
        await ws_server.send_intervention(body.plan)
        confirmation = await _maybe_await_confirmation(
            reg,
            body.plan.intervention_id,
            correlation_id=correlation_id,
            await_confirmation=await_confirmation,
            timeout_seconds=confirmation_timeout_seconds,
        )
        return InterventionApplyResponse(
            applied=True,
            correlation_id=correlation_id,
            confirmation=confirmation,
        )

    return InterventionApplyResponse(
        applied=False, correlation_id=correlation_id,
    )


async def _maybe_await_confirmation(
    reg: Any,
    intervention_id: str,
    *,
    correlation_id: str | None,
    await_confirmation: bool,
    timeout_seconds: float,
) -> InterventionApplyResult | None:
    """F05 helper: bridge the route to the daemon's
    ``await_apply_confirmation`` future. Returns ``None`` if the daemon is
    not registered (legacy test rigs that mock the registry without a
    daemon) or when ``await_confirmation`` is False — in the latter case
    the caller polls separately using ``correlation_id``."""
    if not await_confirmation:
        return None
    daemon = reg.get("daemon") if hasattr(reg, "get") else None
    if daemon is None or not hasattr(daemon, "await_apply_confirmation"):
        return None
    try:
        return await daemon.await_apply_confirmation(
            intervention_id,
            timeout_seconds=timeout_seconds,
            correlation_id=correlation_id,
        )
    except Exception:
        # B9 (Phase 4.1): elevate to WARNING with structured fields so
        # operators see when the apply-confirmation future was
        # cancelled (e.g. by daemon stop) or raised a non-timeout
        # error. correlation_id + intervention_id let log aggregators
        # join the failure back to the originating HTTP call.
        logger.warning(
            "await_apply_confirmation failed",
            extra={
                "intervention_id": intervention_id,
                "correlation_id": correlation_id,
            },
            exc_info=True,
        )
        return None


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
            ws_server = reg.get("ws_server")
            if ws_server is not None and hasattr(ws_server, "send_restore"):
                await ws_server.send_restore(
                    body.intervention_id,
                    user_action=body.user_action,
                )
            return InterventionRestoreResponse(
                restored=outcome.workspace_restored,
                outcome=outcome,
            )

    return InterventionRestoreResponse(restored=False)


# =============================================================================
# v2.0 Endpoints — Stress, Helpfulness, Projects
# =============================================================================


class StressIntegralResponse(BaseModel):
    """Stress integral current value."""
    current_value: float = 0.0
    threshold: float = 500.0
    should_break: bool = False
    sensitivity_multiplier: float = 1.0
    timestamp: float = Field(
        default_factory=time.time,
        description="Wall-clock seconds since epoch (UTC).",
    )


@router.get("/api/stress-integral", response_model=StressIntegralResponse)
async def get_stress_integral(request: Request) -> StressIntegralResponse:
    """Get current stress integral value and break recommendation."""
    reg = _get_registry(request)
    tracker = reg.get("stress_integral_tracker")
    if tracker is not None:
        data = tracker.to_dict()
        return StressIntegralResponse(
            current_value=data.get("current_value", 0.0),
            threshold=data.get("threshold", 500.0),
            should_break=tracker.should_break(),
            sensitivity_multiplier=data.get("sensitivity_multiplier", 1.0),
        )
    return StressIntegralResponse()


class HelpfulnessSummaryResponse(BaseModel):
    """Summary of helpfulness metrics."""
    total_interventions: int = 0
    mean_reward: float = 0.0
    engagement_rate: float = 0.0
    recent_rewards: list[float] = Field(default_factory=list)
    timestamp: float = Field(
        default_factory=time.time,
        description="Wall-clock seconds since epoch (UTC).",
    )


@router.get("/api/helpfulness/summary", response_model=HelpfulnessSummaryResponse)
async def get_helpfulness_summary(request: Request) -> HelpfulnessSummaryResponse:
    """Get helpfulness metrics summary."""
    reg = _get_registry(request)
    tracker = reg.get("helpfulness_tracker")
    if tracker is not None and hasattr(tracker, "get_summary"):
        summary = await tracker.get_summary()
        return HelpfulnessSummaryResponse(**summary)
    return HelpfulnessSummaryResponse()


# =============================================================================
# Consent Endpoints
# =============================================================================


class ConsentLevelResponse(BaseModel):
    """Current consent state."""
    levels: dict[str, dict] = Field(default_factory=dict)
    timestamp: float = Field(
        default_factory=time.time,
        description="Wall-clock seconds since epoch (UTC).",
    )


class ConsentResetRequest(BaseModel):
    """Phase 4.4 T4: explicit (currently empty) body for ``POST
    /consent/reset``.

    Defined so the OpenAPI spec advertises a request schema (rather
    than implicit ``Body(None)``) and TS codegen emits a typed shape.
    Future fields (e.g. ``reason``, ``actor``) can be added without
    breaking existing callers because every field is optional.
    """


class ConsentResetResponse(BaseModel):
    """Result of consent reset."""
    reset: bool = False
    levels: dict[str, dict] = Field(default_factory=dict)
    timestamp: float = Field(
        default_factory=time.time,
        description="Wall-clock seconds since epoch (UTC).",
    )


@router.get("/consent/level", response_model=ConsentLevelResponse)
async def get_consent_level(request: Request) -> ConsentLevelResponse:
    """Get current consent ladder state for all action types."""
    reg = _get_registry(request)
    ladder = reg.get("consent_ladder")
    if ladder is not None and hasattr(ladder, "get_all_states"):
        states = await ladder.get_all_states()
        return ConsentLevelResponse(levels=states)
    return ConsentLevelResponse()


@router.post("/consent/reset", response_model=ConsentResetResponse)
async def reset_consent(
    request: Request,
    body: ConsentResetRequest | None = None,
) -> ConsentResetResponse:
    """Reset consent ladder to defaults and return new state.

    Phase 4.4 T4: ``body`` is accepted explicitly so the OpenAPI spec
    advertises a request shape (even though it is currently empty);
    callers may continue to send ``{}`` or omit the body entirely.
    """
    _ = body  # currently no fields to apply; reserved for future use
    reg = _get_registry(request)
    ladder = reg.get("consent_ladder")
    if ladder is not None and hasattr(ladder, "reset"):
        await ladder.reset()
        states = await ladder.get_all_states()
        return ConsentResetResponse(reset=True, levels=states)
    return ConsentResetResponse()


# =============================================================================
# P0 §3.15 (HTTP parity): /api/cost — BYOK spend telemetry
# =============================================================================
#
# Phase 4.4 T2: the WS path emits ``COST_RESPONSE`` payloads on every
# plan-finalise event, but there was no HTTP surface for callers that
# don't hold a websocket (the desktop shell's diagnostics tab, support
# tooling, integration tests). This route exposes the same numbers as
# a snapshot. We deliberately do NOT subclass the wire CostResponse in
# ``cortex/libs/schemas/realtime.py`` — that one's field names mirror
# the §3.15 wire spec (``cost_today`` / ``budget_today``); this HTTP
# shape mirrors the Phase 4.4 spec (``total_usd`` / ``session_usd``).


class CostResponse(BaseModel):
    """Phase 4.4 T2: BYOK cost telemetry snapshot.

    Field semantics:

    - ``total_usd``: cumulative USD spent today (matches the LLM cost
      tracker's ``today_total_usd``). The tracker resets at local
      midnight; restarts within the same day preserve the running
      total via the on-disk ledger.
    - ``session_usd``: USD spent since the daemon process started.
      Equal to ``total_usd`` after a fresh restart; smaller than
      ``total_usd`` after midnight rollover within the same daemon
      lifetime.
    - ``prompt_tokens`` / ``completion_tokens``: best-effort counters
      surfaced by the active LLM client when available, else ``0``.
    - ``provider``: active provider key (e.g. ``"anthropic_direct"``,
      ``"bedrock"``); ``"none"`` when BYOK is not configured.
    - ``model``: active model id (e.g. ``"claude-sonnet-4-5"``); empty
      string when no client is registered.
    - ``timestamp``: wall-clock seconds since epoch (UTC).
    """

    total_usd: float = Field(
        default=0.0, ge=0.0,
        description="Cumulative USD spent today by the LLM cost tracker.",
    )
    session_usd: float = Field(
        default=0.0, ge=0.0,
        description=(
            "USD spent since the daemon process started. ``total_usd`` "
            "minus the on-disk ledger value at daemon boot."
        ),
    )
    prompt_tokens: int = Field(
        default=0, ge=0,
        description="Best-effort prompt-token counter (0 if unavailable).",
    )
    completion_tokens: int = Field(
        default=0, ge=0,
        description="Best-effort completion-token counter (0 if unavailable).",
    )
    provider: str = Field(
        default="none",
        description=(
            "Active LLM provider key (``anthropic_direct``, "
            "``bedrock``, ``vertex``, ``rule_based``); ``\"none\"`` "
            "when no cost tracker is registered."
        ),
    )
    model: str = Field(
        default="",
        description=(
            "Active model id (e.g. ``claude-sonnet-4-5``); empty "
            "string when no LLM client is registered."
        ),
    )
    timestamp: float = Field(
        default_factory=time.time,
        description="Wall-clock seconds since epoch (UTC).",
    )


def _resolve_cost_tracker(registry: Any) -> Any | None:
    """Locate the LLM ``CostTracker`` regardless of how it was wired.

    Two registration paths exist in the codebase:

    1. The Anthropic planner attaches a private ``_cost_tracker``
       attribute on the LLM client; that client is registered as
       ``"llm_client"`` (see
       :mod:`cortex.services.runtime_daemon._register_services`).
    2. Future callers may register a tracker directly under
       ``"cost_tracker"`` for unit tests / alt providers.

    We check the explicit key first because it lets test rigs avoid
    constructing a whole planner just to exercise this route.
    """
    direct = registry.get("cost_tracker") if hasattr(registry, "get") else None
    if direct is not None:
        return direct
    llm_client = registry.get("llm_client") if hasattr(registry, "get") else None
    if llm_client is not None:
        return getattr(llm_client, "_cost_tracker", None)
    return None


def _resolve_active_model(registry: Any) -> str:
    """Best-effort lookup of the active model id from the LLM client."""
    llm_client = registry.get("llm_client") if hasattr(registry, "get") else None
    if llm_client is None:
        return ""
    for attr in ("model", "_model", "model_id", "_model_id"):
        val = getattr(llm_client, attr, None)
        if isinstance(val, str) and val:
            return val
    cfg = getattr(llm_client, "config", None)
    if cfg is not None:
        for attr in ("model", "model_id"):
            val = getattr(cfg, attr, None)
            if isinstance(val, str) and val:
                return val
    return ""


def _resolve_active_provider(registry: Any) -> str | None:
    """Best-effort provider key lookup from the daemon's config."""
    daemon = registry.get("daemon") if hasattr(registry, "get") else None
    if daemon is None:
        return None
    cfg = getattr(daemon, "config", None)
    llm_cfg = getattr(cfg, "llm", None) if cfg is not None else None
    provider = getattr(llm_cfg, "provider", None) if llm_cfg is not None else None
    return str(provider) if provider else None


@router.get("/api/cost", response_model=CostResponse)
async def get_cost(request: Request) -> CostResponse:
    """Phase 4.4 T2: snapshot today's BYOK LLM spend.

    Returns a zero-valued ``CostResponse`` with ``provider="none"`` when
    no tracker is registered (BYOK not configured, or no calls yet) —
    deliberately NOT a 404, so the desktop shell can poll the route
    unconditionally without branching on HTTP status.
    """
    reg = _get_registry(request)
    tracker = _resolve_cost_tracker(reg)
    provider = _resolve_active_provider(reg)
    model = _resolve_active_model(reg)

    if tracker is None:
        return CostResponse(
            provider=(provider or "none"),
            model=model,
        )

    total_usd = 0.0
    try:
        total_usd = float(tracker.today_total_usd())
    except Exception:
        logger.debug("/api/cost: today_total_usd failed", exc_info=True)

    # ``session_usd`` is best-effort: trackers may not expose a session
    # baseline; if they do (via ``session_start_total_usd`` set at
    # daemon boot) we subtract it. Otherwise fall back to ``total_usd``
    # so the field is at least populated rather than misleadingly 0.
    session_start = getattr(tracker, "session_start_total_usd", None)
    if isinstance(session_start, (int, float)):
        session_usd = max(0.0, total_usd - float(session_start))
    else:
        session_usd = total_usd

    prompt_tokens = 0
    completion_tokens = 0
    for attr in ("prompt_tokens_today", "total_prompt_tokens"):
        val = getattr(tracker, attr, None)
        if isinstance(val, int):
            prompt_tokens = val
            break
        if callable(val):
            try:
                got = val()
                if isinstance(got, int):
                    prompt_tokens = got
                    break
            except Exception:
                pass
    for attr in ("completion_tokens_today", "total_completion_tokens"):
        val = getattr(tracker, attr, None)
        if isinstance(val, int):
            completion_tokens = val
            break
        if callable(val):
            try:
                got = val()
                if isinstance(got, int):
                    completion_tokens = got
                    break
            except Exception:
                pass

    return CostResponse(
        total_usd=total_usd,
        session_usd=session_usd,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        provider=(provider or "anthropic_direct"),
        model=model,
    )


class ProjectListResponse(BaseModel):
    """List of configured projects."""
    projects: list[dict] = Field(default_factory=list)


@router.get("/api/projects", response_model=ProjectListResponse)
async def list_projects(request: Request) -> ProjectListResponse:
    """List all configured project launch profiles."""
    reg = _get_registry(request)
    launcher = reg.get("project_launcher")
    if launcher is not None and hasattr(launcher, "list_projects"):
        projects = launcher.list_projects()
        return ProjectListResponse(projects=[p.model_dump() if hasattr(p, 'model_dump') else p for p in projects])
    return ProjectListResponse()


class LaunchProjectResponse(BaseModel):
    """Result of launching a project."""
    launched: bool = False
    project_name: str = ""
    errors: list[str] = Field(default_factory=list)


@router.post("/api/launch/{project_name}", response_model=LaunchProjectResponse)
async def launch_project(project_name: str, request: Request) -> LaunchProjectResponse:
    """Launch a project workspace configuration.

    Audit-prod fix (P1-C + P1-E): wrap the launch in a 20 s timeout so
    a wedged AppleScript / subprocess can't tie up a uvicorn worker
    indefinitely. Exception messages are mapped to sanitised categories
    rather than echoed verbatim — the raw text used to leak internal
    paths from osascript / subprocess errors back to the caller.
    """
    import asyncio as _asyncio

    reg = _get_registry(request)
    launcher = reg.get("project_launcher")
    if launcher is None or not hasattr(launcher, "launch"):
        return LaunchProjectResponse(
            launched=False,
            project_name=project_name,
            errors=["No project launcher available"],
        )
    try:
        await _asyncio.wait_for(launcher.launch(project_name), timeout=20.0)
        return LaunchProjectResponse(launched=True, project_name=project_name)
    except TimeoutError:
        logger.warning("Project launch timed out: %s", project_name)
        return LaunchProjectResponse(
            launched=False,
            project_name=project_name,
            errors=["launch_timeout"],
        )
    except FileNotFoundError:
        return LaunchProjectResponse(
            launched=False,
            project_name=project_name,
            errors=["project_not_found"],
        )
    except PermissionError:
        return LaunchProjectResponse(
            launched=False,
            project_name=project_name,
            errors=["permission_denied"],
        )
    except Exception:
        logger.exception("Project launch failed: %s", project_name)
        # Map every unexpected error to a generic category — the raw
        # exception text frequently contains absolute paths from
        # osascript / subprocess that we should not leak to callers.
        return LaunchProjectResponse(
            launched=False,
            project_name=project_name,
            errors=["launch_failed"],
        )


# =============================================================================
# P0 §3.1 / §3.2: Session history + trends (REST parity with the WS messages)
# =============================================================================
#
# These three routes mirror the WS handlers in ``websocket_server.py``. They
# are mounted on the authenticated ``router`` so the capability token
# (``require_capability_token``) is required — identical gating to every
# other mutating Cortex endpoint. The daemon registers itself in the
# service registry under ``"daemon"`` (see ``runtime_daemon._register_services``)
# so we resolve through the same indirection ``/api/launch/<name>`` uses for
# the project launcher.


@router.get("/api/sessions", response_model=SessionListResponse)
async def get_sessions(
    request: Request,
    since: float | None = None,
    limit: int = 30,
) -> SessionListResponse:
    """P0 §3.1: paginated session history listing.

    Query params:
        since: epoch-seconds cursor returned by the previous reply's
            ``next_cursor`` (None for the first page).
        limit: page size; clamped to [1, 100] inside the daemon.
    """
    reg = _get_registry(request)
    daemon = reg.get("daemon")
    if daemon is None or not hasattr(daemon, "list_sessions"):
        return SessionListResponse()
    try:
        return await daemon.list_sessions(since, limit)
    except Exception:
        logger.exception("GET /api/sessions failed")
        return SessionListResponse()


@router.get("/api/sessions/{session_id}", response_model=SessionDetailResponse)
async def get_session_detail(
    request: Request,
    session_id: str,
) -> SessionDetailResponse:
    """P0 §3.1: full ``SessionReport`` for one id.

    The daemon validates ``session_id`` against the safe-char regex
    before constructing any filesystem path (defense vs path
    traversal). A missing / unparsable file returns
    ``{report: None, error: "not_found"|"unreadable"}``.
    """
    reg = _get_registry(request)
    daemon = reg.get("daemon")
    if daemon is None or not hasattr(daemon, "get_session"):
        return SessionDetailResponse(report=None, error="not_found")
    try:
        return await daemon.get_session(session_id)
    except Exception:
        logger.exception("GET /api/sessions/{} failed", session_id)
        return SessionDetailResponse(report=None, error="unreadable")


@router.get("/api/trends", response_model=TrendsResponse)
async def get_trends_route(
    request: Request,
    window: Literal["week", "month", "quarter"] = "week",
    refresh: bool = False,
) -> TrendsResponse:
    """P0 §3.2: longitudinal trend / chronotype rollup.

    Query params:
        window: ``"week"`` (last 7 days), ``"month"`` (last 30), or
            ``"quarter"`` (last 90).
        refresh: when True, forces a recompute from disk before
            returning (slower but always-fresh). Defaults to False
            so the dashboard serves the cached ``model.json``.
    """
    reg = _get_registry(request)
    daemon = reg.get("daemon")
    if daemon is None or not hasattr(daemon, "get_trends"):
        # Empty placeholder so the UI can still render a "no data yet"
        # state without crashing.
        return TrendsResponse(window=window)
    try:
        return await daemon.get_trends(window, refresh=refresh)
    except Exception:
        logger.exception("GET /api/trends failed (window=%s)", window)
        return TrendsResponse(window=window)


# =============================================================================
# P0 §3.24: Feedback / bug-report endpoint
# =============================================================================


class FeedbackRequest(BaseModel):
    """P0 §3.24: bug-report payload from the desktop shell.

    The shell composes this when the user opens the "Send feedback" sheet.
    Length bounds match the dashboard's UX (10–500 chars on description).
    """

    description: str = Field(..., min_length=10, max_length=500)
    include_logs: bool = Field(default=False)
    app_version: str = Field(default="", max_length=64)


class FeedbackResponse(BaseModel):
    """P0 §3.24: feedback acknowledgement."""

    ok: bool = True
    report_id: str = ""
    timestamp: float = Field(default_factory=time.time)


# Patterns redacted from bundled log tail. Two scrub passes are applied:
# (1) the auth-token header value, (2) absolute home-directory paths.
# Pre-compiled here so the route handler does not pay the cost on every
# request.
import re as _re  # noqa: E402  (placement keeps imports near use site)

_FEEDBACK_AUTH_HEADER_RE = _re.compile(
    r"(?i)(x-cortex-auth\s*[:=]\s*)\S+"
)
_FEEDBACK_USER_PATH_RE = _re.compile(r"/Users/[^/\s'\")]+")

# B5 (Phase 4.1): module-level counter of bug-report log-tail read
# failures. Surfaced on /health so operators can spot a rotated /
# permission-denied log path that's silently breaking feedback bundles.
_feedback_log_read_failures: int = 0


def _scrub_log_tail(lines: list[str]) -> list[str]:
    """Apply the §3.24 PII scrubs in place; return the cleaned list."""
    out: list[str] = []
    for line in lines:
        cleaned = _FEEDBACK_AUTH_HEADER_RE.sub(r"\1[REDACTED]", line)
        cleaned = _FEEDBACK_USER_PATH_RE.sub("/Users/[REDACTED]", cleaned)
        out.append(cleaned)
    return out


@router.post("/api/feedback", response_model=FeedbackResponse)
async def submit_feedback(
    body: FeedbackRequest,
    request: Request,
) -> FeedbackResponse:
    """P0 §3.24: persist a user-submitted feedback / bug report.

    Mounted on the capability-token-gated router (same as every other
    mutating endpoint). Persists JSON via :func:`atomic_write_json` so a
    SIGKILL mid-write never produces a half-written report. When
    ``include_logs`` is True, the last 1000 lines of
    ``~/Library/Logs/Cortex/cortex_daemon.log`` are bundled with the
    record, after two PII-scrub passes.
    """
    import uuid as _uuid
    from datetime import datetime as _dt
    from pathlib import Path as _Path

    from cortex.libs.utils.atomic_write import atomic_write_json
    from cortex.libs.utils.platform import get_config_dir

    report_id = _uuid.uuid4().hex
    ts = _dt.now()
    record: dict[str, Any] = {
        "report_id": report_id,
        "submitted_at": ts.isoformat(timespec="seconds"),
        "description": body.description,
        "include_logs": bool(body.include_logs),
        "app_version": body.app_version or "",
    }

    if body.include_logs:
        log_path = _Path.home() / "Library" / "Logs" / "Cortex" / "cortex_daemon.log"
        try:
            if log_path.exists():
                lines = log_path.read_text(
                    encoding="utf-8", errors="replace",
                ).splitlines()[-1000:]
                record["log_tail"] = _scrub_log_tail(lines)
        except OSError as exc:
            # B5 (Phase 4.1): elevate to WARNING with structured fields.
            # A user opted into log-bundling and the read failed —
            # operators need to know the bug-report tail will be empty.
            global _feedback_log_read_failures
            _feedback_log_read_failures += 1
            logger.warning(
                "feedback: failed to read log tail",
                extra={
                    "path": str(log_path),
                    "errno": getattr(exc, "errno", -1),
                    "feedback_log_read_failures": _feedback_log_read_failures,
                },
                exc_info=True,
            )

    try:
        feedback_dir = get_config_dir() / "feedback"
        feedback_dir.mkdir(parents=True, exist_ok=True)
        stamp = ts.strftime("%Y%m%dT%H%M%S")
        path = feedback_dir / f"{stamp}_{report_id}.json"
        atomic_write_json(path, record)
    except OSError:
        logger.exception("POST /api/feedback failed to persist")
        return FeedbackResponse(ok=False, report_id=report_id)

    return FeedbackResponse(ok=True, report_id=report_id)
