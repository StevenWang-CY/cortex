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
  GET  /privacy/context/status — Inspect local/external planner posture
  POST /privacy/context/preview — Prepare an exact one-time outbound preview
  POST /privacy/context/preview/current — Preview the current daemon snapshot
  POST /privacy/context/confirm — Send one exact prepared payload once
  DELETE /privacy/context/preview/{id} — Burn a preview without sending

LLM & Intervention:
  POST /llm/plan              — Request intervention plan
  POST /intervention/apply    — Apply intervention to workspace
  POST /intervention/restore  — Restore workspace to pre-intervention state
  POST /intervention/restore-all — Restore every Cortex-owned workspace effect

Status & Health:
  GET  /status/current        — Current system state, confidence, signal quality
  GET  /health                — Health check for all services
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path as FilePath
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Path, Request
from fastapi.responses import Response

from cortex.application.clock import Clock, clock_or_system, utc_datetime
from cortex.libs.config.ports import HTTP_API_PORT, WEBSOCKET_PORT
from cortex.libs.logging.correlation import get_correlation_id
from cortex.libs.logging.structured import EventType
from cortex.libs.ports.intervention_port import InterventionPort
from cortex.libs.schemas.api import (
    AckResponse,
    ConsentLevelResponse,
    ConsentResetRequest,
    ConsentResetResponse,
    ContextBuildRequest,
    ContextBuildResponse,
    DashboardRaiseRequest,
    DashboardRaiseResponse,
    EmergencyRestoreResponse,
    FeedbackRequest,
    FeedbackResponse,
    HealthResponse,
    HelpfulnessSummaryResponse,
    InterventionApplyRequest,
    InterventionApplyResponse,
    InterventionRestoreRequest,
    InterventionRestoreResponse,
    LaunchProjectResponse,
    LLMPlanRequest,
    LLMPlanResponse,
    ProjectListResponse,
    ShutdownResponse,
    StateInferRequest,
    StateInferResponse,
    StatusResponse,
    StressIntegralResponse,
)
from cortex.libs.schemas.context import TaskContext
from cortex.libs.schemas.features import (
    FrameMeta,
    KinematicFeatures,
    PhysioFeatures,
    TelemetryFeatures,
)
from cortex.libs.schemas.intervention import (
    InterventionApplyResult,
    InterventionPlan,
    WorkspaceSnapshot,
)
from cortex.libs.schemas.policy import (
    MRTAnalysisRequest,
    MRTAnalysisResponse,
    MRTExportRequest,
    MRTExportResponse,
    PolicyDiagnosticsRequest,
    PolicyDiagnosticsResponse,
    policy_payload_sha256,
)
from cortex.libs.schemas.privacy import (
    ContextPreviewCancellationResponse,
    ContextPreviewConfirmationRequest,
    ContextPreviewConfirmationResponse,
    ContextPreviewRequest,
    ContextPreviewResponse,
    ContextPrivacyStatusResponse,
    CurrentContextPreviewRequest,
)
from cortex.libs.schemas.realtime import CostResponse
from cortex.libs.schemas.session_history import (
    SessionDetailResponse,
    SessionListResponse,
    TrendsResponse,
)
from cortex.libs.schemas.state import StateEstimate, StateScores
from cortex.libs.schemas.storage import (
    StorageDeleteRequest,
    StorageDeleteResponse,
    StorageExportRequest,
    StorageExportResponse,
    StorageHealthReport,
    StorageStatusResponse,
)
from cortex.libs.schemas.temporal import EventTime
from cortex.libs.schemas.ws_message_types import MessageType
from cortex.services.eval.policy_diagnostics import generate_daily_policy_diagnostics
from cortex.services.eval.research_analysis import (
    ResearchExportError,
    analyze_mrt_export,
    export_mrt_dataset,
    write_mrt_analysis_report,
)
from cortex.services.eval.research_policy import (
    RESEARCH_POLICY_NAME,
    RESEARCH_POLICY_VERSION,
)
from cortex.services.intervention_engine import (
    capture_snapshot as _engine_capture_snapshot,
)
from cortex.services.intervention_engine import (
    materialize_suggestion_only,
)
from cortex.services.intervention_engine import (
    prepare_plan as _engine_prepare_plan,
)
from cortex.services.llm_engine.context_broker import (
    ExternalContextDisabledError,
    PreviewAuthorizationError,
    provider_retention_disclosure,
)
from cortex.storage.maintenance import ActiveInterventionDataError


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
            context,
            intervention_id,
            timestamp=timestamp,
        )
    return _engine_capture_snapshot(
        context,
        intervention_id=intervention_id,
        timestamp=timestamp,
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


def _get_clock(request: Request) -> Clock:
    """Resolve the app-owned clock, falling back only at the HTTP boundary."""

    state = getattr(getattr(request, "app", None), "state", None)
    candidate = getattr(state, "clock", None) if state is not None else None
    return clock_or_system(candidate)


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
    return ShutdownResponse.from_clock(
        _get_clock(request),
        status="shutting_down",
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
            return DashboardRaiseResponse.from_clock(
                _get_clock(request),
                raised=False,
                target=target,
            )
    return DashboardRaiseResponse.from_clock(
        _get_clock(request),
        raised=True,
        target=target,
    )


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


class _NullRegistry:
    """Fallback registry returned when ``app.state.registry`` is absent.

    All ``get`` calls return ``None`` and every read-only property
    returns a safe empty/falsy value so route handlers degrade gracefully
    (return empty/disabled response) rather than raising ``AttributeError``.
    P1-2: replaces the bare attribute access that crashed routes when
    ``app.state.registry`` was not set (e.g. lightweight test rigs).
    """

    def get(self, *_: Any) -> None:
        return None

    @property
    def registered_services(self) -> list[str]:
        return []

    @property
    def healthy(self) -> bool:
        return False


_EMPTY_REGISTRY = _NullRegistry()


def _get_registry(request: Request) -> Any:
    """Get the service registry from app state.

    P1-2: falls back to ``_EMPTY_REGISTRY`` (a null-object that returns
    ``None`` for every ``get`` call) when ``app.state.registry`` is not
    set, so endpoints degrade gracefully instead of raising ``AttributeError``.
    """
    return getattr(request.app.state, "registry", None) or _EMPTY_REGISTRY


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
        context,
        intervention_id=plan.intervention_id,
        request=request,
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
    capture_stale = False
    camera_recovery_attempts = 0
    camera_recovery_successes = 0
    store_degraded = False
    storage_report: StorageHealthReport | None = None
    daemon = reg.get("daemon") if hasattr(reg, "get") else None
    if daemon is not None:
        duplicate_acks = int(getattr(daemon, "_duplicate_intervention_ack_count", 0) or 0)
        store_degraded = bool(getattr(daemon, "_store_degraded", False))
        capture_stale = bool(getattr(daemon, "_capture_stale", False))
        pipeline = getattr(daemon, "_capture_pipeline", None)
        if pipeline is not None:
            frames_dropped_total = int(getattr(pipeline, "frames_dropped_total", 0) or 0)
            capture_stale = bool(getattr(pipeline, "capture_stale", capture_stale))
            camera_recovery_attempts = int(
                getattr(pipeline, "camera_recovery_attempts", 0) or 0
            )
            camera_recovery_successes = int(
                getattr(pipeline, "camera_recovery_successes", 0) or 0
            )
    storage_maintenance = reg.get("storage_maintenance") if hasattr(reg, "get") else None
    if storage_maintenance is not None and hasattr(storage_maintenance, "health"):
        try:
            storage_report = await storage_maintenance.health()
            store_degraded = store_degraded or storage_report.degraded
        except Exception:
            logger.warning("Storage health probe failed", exc_info=True)
            store_degraded = True

    clock = _get_clock(request)
    app_state = getattr(getattr(request, "app", None), "state", None)
    started_mono_ns = getattr(app_state, "started_at_mono_ns", None)
    started_boot_id = getattr(app_state, "started_boot_id", None)
    if isinstance(started_mono_ns, int) and started_boot_id == clock.boot_id:
        uptime_seconds = max(0.0, (clock.monotonic_ns() - started_mono_ns) / 1e9)
    else:
        uptime_seconds = 0.0
    return HealthResponse.from_clock(
        clock,
        status=overall,
        services=services,
        uptime_seconds=uptime_seconds,
        version=_resolve_daemon_version(),
        duplicate_intervention_acks=duplicate_acks,
        frames_dropped_total=frames_dropped_total,
        capture_stale=capture_stale,
        camera_recovery_attempts=camera_recovery_attempts,
        camera_recovery_successes=camera_recovery_successes,
        store_degraded=store_degraded,
        storage=storage_report,
        feedback_log_read_failures=int(_feedback_log_read_failures),
    )


@router.get("/storage/status", response_model=StorageStatusResponse)
async def storage_status(request: Request) -> StorageStatusResponse:
    """Return authenticated persistence settings and a live integrity probe."""

    reg = _get_registry(request)
    maintenance = reg.get("storage_maintenance")
    if maintenance is None or not hasattr(maintenance, "health"):
        raise HTTPException(status_code=503, detail="storage_unavailable")
    report = await maintenance.health()
    return StorageStatusResponse.from_clock(
        _get_clock(request),
        storage=report,
        retention_days=dict(getattr(maintenance, "retention_days", {})),
    )


@router.post("/storage/export", response_model=StorageExportResponse)
async def storage_export(
    body: StorageExportRequest,
    request: Request,
) -> StorageExportResponse:
    """Create a local, checksummed data export in Cortex's exports folder."""

    maintenance = _get_registry(request).get("storage_maintenance")
    if maintenance is None or not hasattr(maintenance, "export"):
        raise HTTPException(status_code=503, detail="storage_unavailable")
    return StorageExportResponse.model_validate(await maintenance.export(body.categories))


@router.post("/storage/delete", response_model=StorageDeleteResponse)
async def storage_delete(
    body: StorageDeleteRequest,
    request: Request,
) -> StorageDeleteResponse:
    """Delete confirmed local scopes without erasing active restore evidence."""

    maintenance = _get_registry(request).get("storage_maintenance")
    if maintenance is None or not hasattr(maintenance, "delete"):
        raise HTTPException(status_code=503, detail="storage_unavailable")
    try:
        deleted, vacuumed = await maintenance.delete(body.scopes)
    except ActiveInterventionDataError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return StorageDeleteResponse.from_clock(
        _get_clock(request),
        deleted_counts=deleted,
        vacuumed=vacuumed,
    )


@router.post("/policy/diagnostics", response_model=PolicyDiagnosticsResponse)
async def policy_diagnostics(
    body: PolicyDiagnosticsRequest,
    request: Request,
) -> PolicyDiagnosticsResponse:
    """Generate an explicitly descriptive, non-causal lifecycle report."""

    repository = _get_registry(request).get("policy_repository")
    configured = getattr(request.app.state, "cortex_config", None)
    if repository is None or configured is None:
        raise HTTPException(status_code=503, detail="policy_repository_unavailable")
    path = await generate_daily_policy_diagnostics(
        repository,
        configured.storage.path,
        day=body.day_utc,
    )
    payload = await asyncio.to_thread(path.read_bytes)
    return PolicyDiagnosticsResponse.from_clock(
        _get_clock(request),
        filename=path.name,
        sha256=hashlib.sha256(payload).hexdigest(),
    )


@router.post("/research/mrt/export", response_model=MRTExportResponse)
async def research_mrt_export(
    body: MRTExportRequest,
    request: Request,
) -> MRTExportResponse:
    """Export one explicitly enabled and prespecified local MRT epoch."""

    repository = _get_registry(request).get("policy_repository")
    configured = getattr(request.app.state, "cortex_config", None)
    if repository is None or configured is None:
        raise HTTPException(status_code=503, detail="policy_repository_unavailable")
    research = configured.eval.research
    specification = body.specification
    if not research.enabled or configured.eval.policy != "research_randomized":
        raise HTTPException(status_code=409, detail="research_mode_not_enabled")
    expected = research.mrt_specification(
        policy_name=RESEARCH_POLICY_NAME,
        policy_version=RESEARCH_POLICY_VERSION,
        reward_version=configured.eval.outcome.reward_version,
        proximal_window_seconds=configured.eval.outcome.reward_window_seconds,
    )
    if specification != expected:
        raise HTTPException(status_code=409, detail="research_specification_mismatch")
    destination_root = FilePath(configured.storage.path).expanduser().resolve() / "research-exports"
    try:
        path = await export_mrt_dataset(
            repository,
            specification,
            destination_root,
            clock=_get_clock(request),
        )
    except ResearchExportError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    payload = await asyncio.to_thread(path.read_bytes)
    document = json.loads(payload)
    return MRTExportResponse.from_clock(
        _get_clock(request),
        filename=path.name,
        sha256=hashlib.sha256(payload).hexdigest(),
        specification_sha256=policy_payload_sha256(specification.model_dump(mode="json")),
        row_count=int(document.get("row_count", 0)),
    )


@router.post("/research/mrt/analyze", response_model=MRTAnalysisResponse)
async def research_mrt_analyze(
    body: MRTAnalysisRequest,
    request: Request,
) -> MRTAnalysisResponse:
    """Recompute WCLS and cluster uncertainty from an immutable export."""

    configured = getattr(request.app.state, "cortex_config", None)
    if configured is None:
        raise HTTPException(status_code=503, detail="configuration_unavailable")
    root = FilePath(configured.storage.path).expanduser().resolve() / "research-exports"
    source = (root / body.filename).resolve()
    if source.parent != root or not source.is_file():
        raise HTTPException(status_code=404, detail="research_export_not_found")
    report = root / f"mrt_analysis_{source.stem}.md"
    try:
        result = await asyncio.to_thread(analyze_mrt_export, source)
        await asyncio.to_thread(write_mrt_analysis_report, source, report)
    except ResearchExportError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return MRTAnalysisResponse.from_clock(
        _get_clock(request),
        source_filename=source.name,
        report_filename=report.name,
        analysis=result.to_dict(),
    )


@health_router.get("/metrics")
async def prometheus_metrics() -> Response:
    """P1-19: Prometheus metrics endpoint.

    Serves the full prometheus_client default registry in the standard
    text exposition format (text/plain; version=0.0.4).  Mounted on the
    public ``health_router`` (no auth) so a Prometheus scraper can reach
    it without a capability token.

    Guaranteed metrics:
      cortex_ws_coalesce_drops_total
      cortex_keyring_timeouts_total
      cortex_state_transitions_total{from_state, to_state}
      cortex_interventions_applied_total{action_type, consent_level}
      cortex_daemon_uptime_seconds
    """
    import prometheus_client

    from cortex.libs.observability import (
        metrics as _m,  # noqa: F401 — side-effect import registers metrics
    )

    data = prometheus_client.generate_latest(prometheus_client.REGISTRY)
    return Response(
        content=data,
        media_type=prometheus_client.CONTENT_TYPE_LATEST,
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
            return StatusResponse.from_clock(
                _get_clock(request),
                status="ready",
                state=est.state,
                support_state=est.support_state,
                estimate_status=est.status,
                confidence=est.confidence,
                evidence_strength=est.confidence,
                evidence_coverage=est.evidence_coverage,
                model=est.model,
                signal_quality=est.signal_quality,
            )

    # Try to get from stored latest
    latest_state = reg.get("latest_state_estimate")
    if latest_state is not None:
        return StatusResponse.from_clock(
            _get_clock(request),
            status="ready",
            state=latest_state.state,
            support_state=latest_state.support_state,
            estimate_status=latest_state.status,
            confidence=latest_state.confidence,
            evidence_strength=latest_state.confidence,
            evidence_coverage=latest_state.evidence_coverage,
            model=latest_state.model,
            signal_quality=latest_state.signal_quality,
        )

    return StatusResponse.from_clock(_get_clock(request), status="initializing")


# =============================================================================
# Capture & Features
# =============================================================================


@router.post("/capture/frame_meta", response_model=AckResponse)
async def submit_frame_meta(
    frame_meta: FrameMeta,
    request: Request,
) -> AckResponse:
    """Submit frame metadata from capture service."""
    reg = _get_registry(request)

    # Store latest frame meta
    reg.register("latest_frame_meta", frame_meta)

    # Forward to any subscribed services
    capture_handler = reg.get("capture_handler")
    if capture_handler is not None and callable(capture_handler):
        await capture_handler(frame_meta)

    return AckResponse.from_clock(_get_clock(request))


@router.post("/features/physio", response_model=AckResponse)
async def submit_physio_features(
    features: PhysioFeatures,
    request: Request,
) -> AckResponse:
    """Submit physio features from physio engine."""
    reg = _get_registry(request)

    reg.register("latest_physio", features)

    # Forward to feature fusion if available
    fusion = reg.get("feature_fusion")
    if fusion is not None and hasattr(fusion, "update_physio"):
        fusion.update_physio(features)

    return AckResponse.from_clock(_get_clock(request))


@router.post("/features/kinematics", response_model=AckResponse)
async def submit_kinematic_features(
    features: KinematicFeatures,
    request: Request,
) -> AckResponse:
    """Submit kinematic features from kinematics engine."""
    reg = _get_registry(request)

    reg.register("latest_kinematics", features)

    fusion = reg.get("feature_fusion")
    if fusion is not None and hasattr(fusion, "update_kinematics"):
        fusion.update_kinematics(features)

    return AckResponse.from_clock(_get_clock(request))


@router.post("/features/telemetry", response_model=AckResponse)
async def submit_telemetry_features(
    features: TelemetryFeatures,
    request: Request,
) -> AckResponse:
    """Submit telemetry features from telemetry engine."""
    reg = _get_registry(request)

    reg.register("latest_telemetry", features)

    fusion = reg.get("feature_fusion")
    if fusion is not None and hasattr(fusion, "update_telemetry"):
        fusion.update_telemetry(features)

    return AckResponse.from_clock(_get_clock(request))


# =============================================================================
# State Inference
# =============================================================================


@router.post("/state/infer", response_model=StateInferResponse)
async def infer_state(
    body: StateInferRequest,
    request: Request,
) -> StateInferResponse:
    """Compute state from fused features.

    Two paths report distinct envelope shapes. The deterministic
    happy path stamps ``source="rules"``; the
    fallback path stamps ``source="fallback"`` and ``degraded=True`` and
    emits :data:`EventType.STATE_INFER_DEGRADED` with the bound
    correlation id. A scorer/smoother exception is treated identically
    to the not-registered case — surfacing a synthetic confidence as if
    it were real is exactly the failure mode the audit flagged.
    """
    reg = _get_registry(request)

    # Try to use registered scorer + smoother
    inference = reg.get("support_inference")
    scorer = reg.get("rule_scorer")
    smoother = reg.get("score_smoother")

    if (inference is not None or scorer is not None) and smoother is not None:
        try:
            evaluation = (
                inference.evaluate(body.feature_vector)
                if inference is not None
                else (
                    scorer.evaluate(body.feature_vector)
                    if hasattr(scorer, "evaluate")
                    else scorer.compute_scores(body.feature_vector)
                )
            )
            feature = body.feature_vector
            event_time = (
                EventTime(
                    observed_at_unix_ms=feature.observed_at_unix_ms,
                    observed_at_mono_ns=feature.observed_at_mono_ns,
                    boot_id=feature.boot_id,
                )
                if feature.observed_at_unix_ms is not None
                and feature.observed_at_mono_ns is not None
                and feature.boot_id is not None
                else EventTime.from_clock(_get_clock(request))
            )
            estimate = smoother.update(
                evaluation,
                body.signal_quality,
                event_time=event_time,
            )
        except Exception:
            # F18: scorer/smoother raised — fall through to the synthetic
            # estimate but flag the response as degraded so the UI can
            # show a banner instead of silently believing a 0.5
            # confidence is authoritative.
            logger.exception("rule scorer / smoother raised; serving fallback estimate")
        else:
            reg.register("latest_state_estimate", estimate)
            return StateInferResponse.from_clock(
                _get_clock(request),
                estimate=estimate,
                source="rules",
                degraded=estimate.status != "estimated",
            )

    # Fallback: produce a basic estimate without engines. Emit the
    # degradation telemetry so a log aggregator sees the failure even if
    # the response body is not inspected.
    logger.warning(
        "%s reason=%s cid=%s",
        EventType.STATE_INFER_DEGRADED.value,
        "scorer_or_smoother_missing"
        if (inference is None and scorer is None) or smoother is None
        else "scorer_raised",
        get_correlation_id() or "-",
    )
    estimate = StateEstimate(
        state="UNKNOWN",
        support_state="unknown",
        status="insufficient_evidence",
        confidence=0.0,
        scores=StateScores(),
        evidence_coverage=0.0,
        reasons=["State engine unavailable; no support estimate was produced"],
        signal_quality=body.signal_quality,
        timestamp=float(body.feature_vector.__dict__.get("timestamp", 0.0)),
        observed_at_unix_ms=body.feature_vector.observed_at_unix_ms,
        observed_at_mono_ns=body.feature_vector.observed_at_mono_ns,
        boot_id=body.feature_vector.boot_id,
        dwell_seconds=0.0,
    )
    reg.register("latest_state_estimate", estimate)
    return StateInferResponse.from_clock(
        _get_clock(request),
        estimate=estimate,
        source="fallback",
        degraded=True,
    )


# =============================================================================
# Context Building
# =============================================================================


@router.post("/context/build", response_model=ContextBuildResponse)
async def build_context(
    body: ContextBuildRequest,
    request: Request,
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
        return ContextBuildResponse.from_clock(
            _get_clock(request),
            context=ctx,
            available=True,
        )

    return ContextBuildResponse.from_clock(
        _get_clock(request),
        available=False,
    )


# =============================================================================
# LLM Planning
# =============================================================================


def _privacy_planner(registry: Any) -> Any | None:
    """Resolve the one planner that owns the external context boundary."""

    for key in ("llm_engine", "llm_client"):
        candidate = registry.get(key) if hasattr(registry, "get") else None
        if candidate is not None and hasattr(candidate, "generate_intervention_plan"):
            return candidate
    return None


@router.get(
    "/privacy/context/status",
    response_model=ContextPrivacyStatusResponse,
)
async def context_privacy_status(request: Request) -> ContextPrivacyStatusResponse:
    """Return the effective fail-closed planner and retention posture."""

    reg = _get_registry(request)
    planner = _privacy_planner(reg)
    if planner is not None and hasattr(planner, "privacy_status"):
        result = planner.privacy_status()
        if isinstance(result, ContextPrivacyStatusResponse):
            return result

    daemon = reg.get("daemon") if hasattr(reg, "get") else None
    config = getattr(daemon, "config", None)
    llm_config = getattr(config, "llm", None)
    if llm_config is None:
        raise HTTPException(status_code=503, detail="privacy configuration unavailable")
    return ContextPrivacyStatusResponse.from_clock(
        _get_clock(request),
        planner_mode=llm_config.privacy.planner_mode,
        network_allowed_by_configuration=False,
        pending_previews=0,
        provider=llm_config.provider,
        retention=provider_retention_disclosure(llm_config),
    )


@router.post(
    "/privacy/context/preview",
    response_model=ContextPreviewResponse,
)
async def preview_external_context(
    body: ContextPreviewRequest,
    request: Request,
) -> ContextPreviewResponse:
    """Prepare a bounded payload without making an external request."""

    planner = _privacy_planner(_get_registry(request))
    if planner is None or not hasattr(planner, "preview_external_request"):
        raise HTTPException(
            status_code=409,
            detail="external planning is not enabled",
        )
    try:
        result = await planner.preview_external_request(body)
    except ExternalContextDisabledError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not isinstance(result, ContextPreviewResponse):
        raise HTTPException(status_code=503, detail="invalid context preview result")
    return result


@router.post(
    "/privacy/context/preview/current",
    response_model=ContextPreviewResponse,
)
async def preview_current_external_context(
    body: CurrentContextPreviewRequest,
    request: Request,
) -> ContextPreviewResponse:
    """Preview the daemon's current snapshots without returning raw inputs.

    Desktop clients submit only their source selections.  Raw editor,
    terminal, browser, and inferred-state objects stay inside the daemon and
    enter the broker exactly once.
    """

    reg = _get_registry(request)
    planner = _privacy_planner(reg)
    if planner is None or not hasattr(planner, "preview_external_request"):
        raise HTTPException(status_code=409, detail="external planning is not enabled")
    context = reg.get("latest_task_context") if hasattr(reg, "get") else None
    state = reg.get("latest_state_estimate") if hasattr(reg, "get") else None
    if not isinstance(context, TaskContext) or not isinstance(state, StateEstimate):
        raise HTTPException(
            status_code=409,
            detail="a current workspace and support snapshot is not available yet",
        )
    preview_request = ContextPreviewRequest(
        task_context=context.model_copy(deep=True),
        state_estimate=state.model_copy(deep=True),
        selection=body.selection,
        constraints=body.constraints,
        template_name=body.template_name,
        extra_context=body.extra_context,
    )
    try:
        result = await planner.preview_external_request(preview_request)
    except ExternalContextDisabledError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not isinstance(result, ContextPreviewResponse):
        raise HTTPException(status_code=503, detail="invalid context preview result")
    return result


@router.post(
    "/privacy/context/confirm",
    response_model=ContextPreviewConfirmationResponse,
)
async def confirm_external_context(
    body: ContextPreviewConfirmationRequest,
    request: Request,
) -> ContextPreviewConfirmationResponse:
    """Consume and send one exact prepared payload, then validate the plan."""

    planner = _privacy_planner(_get_registry(request))
    if planner is None or not hasattr(planner, "confirm_external_request"):
        raise HTTPException(status_code=409, detail="external planning is not enabled")
    try:
        plan = await planner.confirm_external_request(
            body.preview_id,
            body.confirmation_phrase,
        )
    except ExternalContextDisabledError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PreviewAuthorizationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="external planner timed out") from exc
    except Exception as exc:
        logger.exception("confirmed external planner request failed")
        raise HTTPException(status_code=503, detail="external planner request failed") from exc
    if not isinstance(plan, InterventionPlan):
        raise HTTPException(status_code=503, detail="invalid planner response")
    return ContextPreviewConfirmationResponse.from_clock(
        _get_clock(request),
        preview_id=body.preview_id,
        plan=plan,
        fallback_used=_plan_served_fallback(plan),
    )


@router.delete(
    "/privacy/context/preview/{preview_id}",
    response_model=ContextPreviewCancellationResponse,
)
async def cancel_external_context_preview(
    preview_id: str,
    request: Request,
) -> ContextPreviewCancellationResponse:
    """Burn one prepared payload without sending it to the provider."""

    if not 20 <= len(preview_id) <= 160 or not preview_id.startswith("ctx_"):
        raise HTTPException(status_code=422, detail="invalid context preview handle")
    planner = _privacy_planner(_get_registry(request))
    if planner is None or not hasattr(planner, "cancel_preview"):
        raise HTTPException(status_code=409, detail="external planning is not enabled")
    cancelled = bool(await planner.cancel_preview(preview_id))
    return ContextPreviewCancellationResponse.from_clock(
        _get_clock(request),
        preview_id=preview_id,
        cancelled=cancelled,
    )


def _planner_request_kwargs(body: LLMPlanRequest) -> dict[str, Any]:
    """Preserve old two-argument test/client adapters unless extras are used."""

    kwargs: dict[str, Any] = {}
    if body.constraints is not None:
        kwargs["constraints"] = body.constraints
    if body.template_name is not None:
        kwargs["template_name"] = body.template_name
    if body.extra_context:
        kwargs["extra_context"] = body.extra_context
    if body.privacy_preview_id is not None:
        kwargs["privacy_preview_id"] = body.privacy_preview_id
    if body.privacy_confirmation is not None:
        kwargs["privacy_confirmation"] = body.privacy_confirmation
    return kwargs


def _plan_served_fallback(plan: Any) -> bool:
    """P1 fix: derive whether a returned plan is a rule-based fallback.

    The route previously hard-coded ``fallback_used=False`` on every
    production branch, so a plan the planner served from its
    deterministic fallback (budget-killed, circuit-open, parse-error,
    retries-exhausted) reported ``fallback_used=False`` over HTTP — the
    exact opposite of the truth, and inconsistent with the WS surface
    which carries ``metadata.source``.

    We reuse :func:`classify_plan_failure_mode` (the single source of
    truth that already folds in ``metadata.source == 'fallback'`` and
    every ``metadata.fallback_reason`` value): any classification other
    than ``"ok"`` means the planner did not return a live LLM plan.
    """
    try:
        from cortex.services.llm_engine.anthropic_planner import (
            classify_plan_failure_mode,
        )

        return classify_plan_failure_mode(plan) != "ok"
    except Exception:
        # Classifier import / inspection failed — fall back to the raw
        # metadata flag so a missing planner module doesn't mislabel a
        # genuine fallback as a live plan.
        meta = getattr(plan, "metadata", None) or {}
        try:
            return str(meta.get("source") or "") == "fallback"
        except Exception:
            return False


@router.post("/llm/plan", response_model=LLMPlanResponse)
async def request_llm_plan(
    body: LLMPlanRequest,
    request: Request,
) -> LLMPlanResponse:
    """Request intervention plan from LLM engine.

    P1 fix (finding #6): the planner call is wrapped in defensive
    error handling. A raising client now returns a 503-style fallback
    envelope (``plan=None``, ``fallback_used=True``) instead of bubbling
    an unhandled exception into an opaque 500. P1 fix (finding #2):
    ``fallback_used`` is derived from the returned plan's classification
    rather than hard-coded False.
    """
    reg = _get_registry(request)

    llm_engine = reg.get("llm_engine")
    try:
        if llm_engine is not None:
            if hasattr(llm_engine, "generate_intervention_plan"):
                # B8 (Phase 4.1): tag the chosen planner branch so
                # operators can diff log distributions across deploys
                # when one branch silently degrades to the wrong fallback.
                logger.info(
                    "LLM planner branch selected",
                    extra={"planner_method": "llm_engine.generate_intervention_plan"},
                )
                plan = await llm_engine.generate_intervention_plan(
                    body.task_context,
                    body.state_estimate,
                    **_planner_request_kwargs(body),
                )
                # B11 (Phase 4.1): inspect the discriminated failure_mode
                # and log a structured entry tagged with the result. The
                # same classification drives ``fallback_used`` so the
                # wire field matches the operator log.
                fallback_used = _plan_served_fallback(plan)
                logger.info(
                    "LLM planner result classified",
                    extra={
                        "planner_method": "llm_engine.generate_intervention_plan",
                        "fallback_used": fallback_used,
                    },
                )
                return LLMPlanResponse.from_clock(
                    _get_clock(request),
                    plan=plan,
                    fallback_used=fallback_used,
                )
            if hasattr(llm_engine, "generate_plan"):
                logger.info(
                    "LLM planner branch selected",
                    extra={"planner_method": "llm_engine.generate_plan"},
                )
                plan = await llm_engine.generate_plan(
                    body.state_estimate,
                    body.task_context,
                )
                return LLMPlanResponse.from_clock(
                    _get_clock(request),
                    plan=plan,
                    fallback_used=_plan_served_fallback(plan),
                )

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
                **_planner_request_kwargs(body),
            )
            return LLMPlanResponse.from_clock(
                _get_clock(request),
                plan=plan,
                fallback_used=_plan_served_fallback(plan),
            )
    except Exception:
        # Finding #6: a raising planner client must not surface an
        # unhandled 500. Map it to the deterministic-fallback envelope so
        # the caller sees ``fallback_used=True`` with no plan — the same
        # shape the no-engine path returns — and the cause is captured in
        # the WARN log with the bound correlation id for triage.
        logger.warning(
            "LLM planner raised; serving fallback envelope",
            extra={"cid": get_correlation_id() or "-"},
            exc_info=True,
        )
        return LLMPlanResponse.from_clock(
            _get_clock(request),
            plan=None,
            fallback_used=True,
        )

    logger.info(
        "LLM planner branch selected",
        extra={"planner_method": "fallback"},
    )
    return LLMPlanResponse.from_clock(
        _get_clock(request),
        fallback_used=True,
    )


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
    """Apply or, in the safe default, present an intervention proposal.

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
    correlation_id = request.headers.get("X-Cortex-Request-ID") if request is not None else None

    # The HTTP route is an authority boundary of its own. Do not rely only on
    # executor-level per-command rejection: suggestion-only must also avoid
    # snapshots, restore registrations, confirmation waits, and an optimistic
    # applied response. Configuration is authoritative in production; daemon
    # and executor fallbacks cover lightweight composition/test rigs.
    configured = getattr(request.app.state, "cortex_config", None)
    raw_mode: object | None = None
    if configured is not None:
        raw_mode = getattr(
            getattr(configured, "intervention", None),
            "execution_mode",
            None,
        )
    daemon = reg.get("daemon")
    if raw_mode is None and daemon is not None:
        raw_mode = getattr(daemon, "intervention_execution_mode", None)
    executor = _get_first_service(reg, "intervention_executor", "executor")
    if raw_mode is None and executor is not None:
        raw_mode = getattr(executor, "execution_mode", None)
    execution_mode = (
        str(raw_mode)
        if raw_mode in {"suggest_only", "authorized", "research_autonomous"}
        else "suggest_only"
    )

    # WP-6 containment: this legacy endpoint has no manifest digest, exact
    # action subset, one-time nonce, or requester boot identity. It therefore
    # cannot grant workspace authority in *any* execution mode. Keep it as a
    # presentation compatibility endpoint; mutation-capable surfaces use the
    # INTERVENTION_AUTHORIZE -> INTERVENTION_APPLY protocol instead.
    validation, _ = prepare_plan(body.plan, request=request)
    if not validation.is_valid:
        logger.warning(
            "Rejected intervention proposal %s: %s",
            body.plan.intervention_id,
            validation.errors,
        )
        return InterventionApplyResponse.from_clock(
            _get_clock(request),
            applied=False,
            correlation_id=correlation_id,
        )
    proposal = materialize_suggestion_only(body.plan)
    ws_server = reg.get("ws_server")
    if ws_server is not None and hasattr(ws_server, "send_intervention"):
        await ws_server.send_intervention(proposal)
    if execution_mode != "suggest_only":
        logger.info(
            "Legacy HTTP apply contained to proposal-only in mode=%s",
            execution_mode,
        )
    return InterventionApplyResponse.from_clock(
        _get_clock(request),
        applied=False,
        correlation_id=correlation_id,
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
        confirmation: InterventionApplyResult | None = await daemon.await_apply_confirmation(
            intervention_id,
            timeout_seconds=timeout_seconds,
            correlation_id=correlation_id,
        )
        return confirmation
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
    body: InterventionRestoreRequest,
    request: Request,
) -> InterventionRestoreResponse:
    """Restore workspace to pre-intervention state."""
    reg = _get_registry(request)

    # The daemon binds RestoreManager to the durable, receipt-verified
    # transaction coordinator. Prefer that stable boundary whenever the live
    # runtime is registered; calling a legacy engine/manager directly would
    # bypass exact inverse dispatch and could report an optimistic restore.
    daemon = reg.get("daemon") if hasattr(reg, "get") else None
    if daemon is not None and hasattr(daemon, "restore_intervention"):
        outcome = await daemon.restore_intervention(
            body.intervention_id,
            body.user_action,
        )
        return InterventionRestoreResponse.from_clock(
            _get_clock(request),
            restored=bool(outcome is not None and outcome.workspace_restored),
            outcome=outcome,
        )

    # No daemon means no durable coordinator, no bound executor routing, and
    # no receipt waiter. Fail closed instead of reviving the pre-WP6 direct
    # executor/RestoreManager bypass.
    return InterventionRestoreResponse.from_clock(
        _get_clock(request),
        restored=False,
    )


@router.post(
    "/intervention/restore-all",
    response_model=EmergencyRestoreResponse,
)
async def restore_all_interventions(
    request: Request,
) -> EmergencyRestoreResponse:
    """Restore every exact Cortex-owned effect without policy or LLM access."""

    reg = _get_registry(request)
    daemon = reg.get("daemon") if hasattr(reg, "get") else None
    if daemon is None or not hasattr(
        daemon,
        "restore_all_transactional_effects",
    ):
        return EmergencyRestoreResponse.from_clock(
            _get_clock(request),
            available=False,
            complete=False,
        )
    summary = await daemon.restore_all_transactional_effects(
        reason="emergency_restore",
        timeout_seconds=3.0,
    )
    return EmergencyRestoreResponse.from_clock(
        _get_clock(request),
        available=True,
        complete=summary["pending"] == 0 and summary["failed"] == 0,
        **summary,
    )


# =============================================================================
# v2.0 Endpoints — Stress, Helpfulness, Projects
# =============================================================================


@router.get("/api/stress-integral", response_model=StressIntegralResponse)
async def get_stress_integral(request: Request) -> StressIntegralResponse:
    """Report unavailability; never turn a diagnostic read into a trigger."""

    return StressIntegralResponse.from_clock(_get_clock(request))


@router.get("/api/helpfulness/summary", response_model=HelpfulnessSummaryResponse)
async def get_helpfulness_summary(request: Request) -> HelpfulnessSummaryResponse:
    """Get helpfulness metrics summary."""
    reg = _get_registry(request)
    tracker = reg.get("helpfulness_tracker")
    if tracker is not None and hasattr(tracker, "get_summary"):
        summary = await tracker.get_summary()
        return HelpfulnessSummaryResponse.from_clock(_get_clock(request), **summary)
    return HelpfulnessSummaryResponse.from_clock(_get_clock(request))


# =============================================================================
# Consent Endpoints
# =============================================================================


@router.get("/consent/level", response_model=ConsentLevelResponse)
async def get_consent_level(request: Request) -> ConsentLevelResponse:
    """Get current consent ladder state for all action types."""
    reg = _get_registry(request)
    ladder = reg.get("consent_ladder")
    if ladder is not None and hasattr(ladder, "get_all_states"):
        states = await ladder.get_all_states()
        return ConsentLevelResponse.from_clock(
            _get_clock(request),
            levels=states,
        )
    return ConsentLevelResponse.from_clock(_get_clock(request))


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
    # Audit forensic-trail: emit the CONSENT_RESET event before
    # mutating the ladder so we record the request even if the reset
    # itself raises mid-flight.
    logger.info(
        "%s cid=%s ladder_present=%s",
        EventType.CONSENT_RESET.value,
        get_correlation_id() or "-",
        ladder is not None,
    )
    if ladder is not None and hasattr(ladder, "reset"):
        await ladder.reset()
        # A global consent reset lowers authority immediately. Exact effects
        # that were already consumed may be in flight, so request their
        # deterministic inverses before acknowledging the reset. Offline
        # owners remain in the durable recovery queue.
        daemon = reg.get("daemon") if hasattr(reg, "get") else None
        if daemon is not None and hasattr(
            daemon,
            "restore_all_transactional_effects",
        ):
            try:
                summary = await daemon.restore_all_transactional_effects(
                    reason="system_cancelled",
                    timeout_seconds=3.0,
                )
                if summary["pending"] or summary["failed"]:
                    logger.warning(
                        "Consent reset retained unresolved exact restores: %s",
                        summary,
                    )
            except Exception:
                logger.exception("Consent reset restore barrier failed; recovery remains durable")
        states = await ladder.get_all_states()
        return ConsentResetResponse.from_clock(
            _get_clock(request),
            reset=True,
            levels=states,
        )
    return ConsentResetResponse.from_clock(_get_clock(request))


# =============================================================================
# P0 §3.15 (HTTP parity): /api/cost — BYOK spend telemetry
# =============================================================================
#
# Phase 4.4 T2: the WS path emits ``COST_RESPONSE`` payloads on every
# plan-finalise event, but there was no HTTP surface for callers that
# don't hold a websocket (the desktop shell's diagnostics tab, support
# tooling, integration tests). This route exposes the same numbers as
# a snapshot, using the canonical CostResponse imported from
# ``cortex.libs.schemas.realtime`` so HTTP and WS share one envelope.


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


def _resolve_daily_cost_budget(registry: Any) -> float:
    """Resolve today's USD budget cap from ``config.llm.daily_cost_budget_usd``.

    P1 fix: the HTTP ``/api/cost`` route previously probed non-existent
    public attributes on :class:`CostTracker`
    (``daily_budget_usd`` / ``kill_usd`` / ``budget_usd``) which always
    resolved to ``None`` → ``budget_today`` was permanently 0.0, breaking
    the "same numbers as WS" contract. The WS path
    (:meth:`CortexDaemon.get_cost_response`) reads the budget from
    ``config.llm.daily_cost_budget_usd``; this helper mirrors that exact
    resolution so the two surfaces agree. Returns 0.0 (unlimited) when no
    daemon / config is wired or the value is non-numeric.
    """
    daemon = registry.get("daemon") if hasattr(registry, "get") else None
    if daemon is None:
        return 0.0
    cfg = getattr(daemon, "config", None)
    llm_cfg = getattr(cfg, "llm", None) if cfg is not None else None
    if llm_cfg is None:
        return 0.0
    try:
        return float(getattr(llm_cfg, "daily_cost_budget_usd", 0.0))
    except (TypeError, ValueError):
        return 0.0


@router.get("/api/cost", response_model=CostResponse)
async def get_cost(request: Request) -> CostResponse:
    """Phase 4.4 T2: snapshot today's BYOK LLM spend.

    Returns a zero-valued ``CostResponse`` with ``provider=None`` (JSON
    null) when no tracker is registered (BYOK not configured, or no calls
    yet) — deliberately NOT a 404, so the desktop shell can poll the route
    unconditionally without branching on HTTP status.

    Uses the canonical ``CostResponse`` from ``cortex.libs.schemas.realtime``
    (same envelope as the WS COST_RESPONSE). Field mapping:
    - ``tracker.today_total_usd()`` → ``cost_today``
    - ``config.llm.daily_cost_budget_usd`` → ``budget_today`` (0.0 means
      unlimited) — identical to the WS path's
      :meth:`CortexDaemon.get_cost_response`.
    - ``tracker.check_budget() == "KILL"`` → ``budget_exhausted``, again
      matching the WS path so the two surfaces never disagree.
    """
    reg = _get_registry(request)
    tracker = _resolve_cost_tracker(reg)
    provider = _resolve_active_provider(reg)
    model = _resolve_active_model(reg) or None

    if tracker is None:
        return CostResponse.from_clock(
            _get_clock(request),
            cost_today=0.0,
            budget_today=0.0,
            provider=provider,  # None when no daemon/config wired
            model=model,
        )

    cost_today = 0.0
    try:
        cost_today = float(tracker.today_total_usd())
    except Exception:
        logger.debug("/api/cost: today_total_usd failed", exc_info=True)

    # budget_today: resolve from config (same source the WS path uses in
    # ``CortexDaemon.get_cost_response``). The CostTracker has NO public
    # ``daily_budget_usd`` / ``kill_usd`` / ``budget_usd`` attribute — the
    # cap lives in ``config.llm.daily_cost_budget_usd``. Probing the
    # tracker for those names always failed, so this surface reported a
    # permanent 0.0 budget that contradicted the WS COST_RESPONSE. 0.0
    # means unlimited.
    budget_today = _resolve_daily_cost_budget(reg)

    # budget_exhausted: derive from the tracker's authoritative budget
    # state machine — the WS path uses ``check_budget() == "KILL"``. We
    # mirror it exactly so HTTP and WS agree on the kill flag, falling
    # back to the spend-vs-cap comparison only when the tracker does not
    # expose ``check_budget`` (alt providers / test doubles).
    budget_exhausted = False
    check_budget = getattr(tracker, "check_budget", None)
    if callable(check_budget):
        try:
            budget_exhausted = bool(check_budget() == "KILL")
        except Exception:
            logger.debug("/api/cost: check_budget failed", exc_info=True)
            if budget_today > 0.0:
                budget_exhausted = cost_today >= budget_today
    elif budget_today > 0.0:
        budget_exhausted = cost_today >= budget_today

    # P2-CONTRACT-2: probe token totals via the SAME shared helper the WS
    # COST_RESPONSE path (CortexDaemon.get_cost_response) uses, so the HTTP
    # and WS surfaces can never diverge on these keys. Imported locally to
    # match the lazy llm_engine import pattern used elsewhere in this module.
    from cortex.services.llm_engine.cost_tracker import probe_token_totals

    prompt_tokens, completion_tokens = probe_token_totals(tracker)

    return CostResponse.from_clock(
        _get_clock(request),
        cost_today=cost_today,
        budget_today=budget_today,
        budget_exhausted=budget_exhausted,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        provider=provider,  # None when no daemon config — never "none"
        model=model,
    )


@router.get("/api/projects", response_model=ProjectListResponse)
async def list_projects(request: Request) -> ProjectListResponse:
    """List all configured project launch profiles."""
    reg = _get_registry(request)
    launcher = reg.get("project_launcher")
    if launcher is not None and hasattr(launcher, "list_projects"):
        projects = launcher.list_projects()
        return ProjectListResponse.from_clock(
            _get_clock(request),
            projects=[p.model_dump() if hasattr(p, "model_dump") else p for p in projects],
        )
    return ProjectListResponse.from_clock(_get_clock(request))


@router.post("/api/launch/{project_name}", response_model=LaunchProjectResponse)
async def launch_project(
    request: Request,
    project_name: str = Path(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9._-]+$",
        description=(
            "P2-1: alphanumeric + dot/underscore/hyphen only; "
            "1–64 chars.  Path-traversal sequences are rejected by "
            "FastAPI before the handler runs."
        ),
    ),
) -> LaunchProjectResponse:
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
        return LaunchProjectResponse.from_clock(
            _get_clock(request),
            launched=False,
            project_name=project_name,
            errors=["No project launcher available"],
        )
    try:
        await _asyncio.wait_for(launcher.launch(project_name), timeout=20.0)
        return LaunchProjectResponse.from_clock(
            _get_clock(request),
            launched=True,
            project_name=project_name,
        )
    except TimeoutError:
        logger.warning("Project launch timed out: %s", project_name)
        return LaunchProjectResponse.from_clock(
            _get_clock(request),
            launched=False,
            project_name=project_name,
            errors=["launch_timeout"],
        )
    except FileNotFoundError:
        return LaunchProjectResponse.from_clock(
            _get_clock(request),
            launched=False,
            project_name=project_name,
            errors=["project_not_found"],
        )
    except PermissionError:
        return LaunchProjectResponse.from_clock(
            _get_clock(request),
            launched=False,
            project_name=project_name,
            errors=["permission_denied"],
        )
    except Exception:
        logger.exception("Project launch failed: %s", project_name)
        # Map every unexpected error to a generic category — the raw
        # exception text frequently contains absolute paths from
        # osascript / subprocess that we should not leak to callers.
        return LaunchProjectResponse.from_clock(
            _get_clock(request),
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
        sessions: SessionListResponse = await daemon.list_sessions(since, limit)
        return sessions
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
    import asyncio as _asyncio

    reg = _get_registry(request)
    daemon = reg.get("daemon")
    if daemon is None or not hasattr(daemon, "get_session"):
        return SessionDetailResponse(report=None, error="not_found")
    try:
        # Hard ceiling so a pathological JSON file can't pin a worker
        # in the asyncio thread pool forever. The daemon delegates the
        # actual read to ``asyncio.to_thread``; the cancellation here
        # frees this request even if the worker is still grinding.
        return await _asyncio.wait_for(
            daemon.get_session(session_id),
            timeout=10.0,
        )
    except TimeoutError:
        # Deviation note: the audit plan asked for ``error="timeout"``,
        # but :class:`SessionDetailResponse.error` is a strict Literal
        # owned by another phase (``cortex/libs/schemas/*``) that does
        # NOT include ``"timeout"``. We map to ``"internal"`` (which
        # IS in the literal) and surface the cause in the WARN log so
        # operators can still distinguish a timeout from a generic
        # callback exception when triaging.
        logger.warning(
            "GET /api/sessions/%s timed out after 10s — mapped to error='internal'",
            session_id,
        )
        return SessionDetailResponse(report=None, error="internal")
    except Exception:
        logger.exception("GET /api/sessions/{} failed", session_id)
        return SessionDetailResponse(report=None, error="unreadable")


@router.get("/api/trends", response_model=TrendsResponse)
async def get_trends_route(
    request: Request,
    window: Literal["week", "month"] = "week",
    refresh: bool = False,
) -> TrendsResponse:
    """P0 §3.2: longitudinal trend / chronotype rollup.

    Query params:
        window: ``"week"`` (last 7 days) or ``"month"`` (last 30).
            The TrendsRequest / TrendsResponse schemas only support
            these two values; "quarter" was a stale doc claim that
            FastAPI accepted at the route boundary but the response
            schema rejected — leading to 500s instead of a 422.
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
        trends: TrendsResponse = await daemon.get_trends(window, refresh=refresh)
        return trends
    except Exception:
        logger.exception("GET /api/trends failed (window=%s)", window)
        return TrendsResponse(window=window)


# =============================================================================
# P0 §3.24: Feedback / bug-report endpoint
# =============================================================================


# Patterns redacted from bundled log tail. Two scrub passes are applied:
# (1) the auth-token header value, (2) absolute home-directory paths.
# Pre-compiled here so the route handler does not pay the cost on every
# request.
import re as _re  # noqa: E402  (placement keeps imports near use site)

_FEEDBACK_AUTH_HEADER_RE = _re.compile(r"(?i)(x-cortex-auth\s*[:=]\s*)\S+")
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
    from pathlib import Path as _Path

    from cortex.libs.utils.atomic_write import atomic_write_json
    from cortex.libs.utils.platform import get_config_dir

    report_id = _uuid.uuid4().hex
    ts = utc_datetime(_get_clock(request))
    record: dict[str, Any] = {
        "report_id": report_id,
        "submitted_at": ts.isoformat(timespec="seconds"),
        "description": body.description,
        "include_logs": bool(body.include_logs),
        "app_version": body.app_version or "",
        # C2: persist the originating browser/OS user-agent so support can
        # triage a report without round-tripping the user.
        "user_agent": body.user_agent or "",
    }

    if body.include_logs:
        log_path = _Path.home() / "Library" / "Logs" / "Cortex" / "cortex_daemon.log"
        try:
            if log_path.exists():
                lines = log_path.read_text(
                    encoding="utf-8",
                    errors="replace",
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
        return FeedbackResponse.from_clock(
            _get_clock(request),
            ok=False,
            report_id=report_id,
        )

    return FeedbackResponse.from_clock(
        _get_clock(request),
        ok=True,
        report_id=report_id,
    )
