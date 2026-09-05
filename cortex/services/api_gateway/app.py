"""
API Gateway — FastAPI Application

FastAPI application with CORS, lifespan events, and service dependency
injection. Provides the HTTP server for Cortex's internal service APIs.

Endpoints:
- Capture & features submission
- State inference
- Context building
- LLM planning
- Intervention control
- Health & status

Configuration: APIConfig (host=127.0.0.1; ports from
``cortex.libs.config.ports``: HTTP_API_PORT / WEBSOCKET_PORT)
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from cortex import __version__
from cortex.application.clock import SYSTEM_CLOCK, Clock
from cortex.application.services import ServiceRegistry as ServiceRegistry
from cortex.libs.config.settings import APIConfig, CortexConfig
from cortex.libs.logging.correlation import correlation_scope
from cortex.services.api_gateway.auth import require_capability_token
from cortex.services.api_gateway.middleware.rate_limit import RateLimitMiddleware
from cortex.services.api_gateway.request_ids import sanitize_correlation_id

_REQUEST_ID_HEADER = "X-Cortex-Request-ID"

logger = logging.getLogger(__name__)


# Compatibility registry for callers that construct ``create_app()`` without
# an explicit composition root. Production passes an instance-owned registry.
registry = ServiceRegistry()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    Application lifespan handler.

    Startup: mark system healthy, log readiness.
    Shutdown: mark unhealthy, clean up services.
    """
    logger.info("Cortex API Gateway starting up")
    services: ServiceRegistry = app.state.registry
    services.healthy = True
    logger.info(
        "Services registered: %s",
        services.registered_services,
    )
    try:
        yield
    finally:
        logger.info("Cortex API Gateway shutting down")
        services.healthy = False


def create_app(
    config: APIConfig | None = None,
    cortex_config: CortexConfig | None = None,
    *,
    clock: Clock | None = None,
    services: ServiceRegistry | None = None,
) -> FastAPI:
    """
    Create and configure the FastAPI application.

    Args:
        config: API configuration. Defaults to APIConfig().
        cortex_config: Full Cortex configuration for service initialization.
        clock: Explicit wall/monotonic clock shared with the application.
        services: Instance-scoped application services. The module-level
            registry is retained only as a compatibility default.

    Returns:
        Configured FastAPI application.
    """
    cfg = config or APIConfig()
    app_clock = clock or SYSTEM_CLOCK
    app_services = services if services is not None else registry

    app = FastAPI(
        title="Cortex API Gateway",
        description="Somatic Workspace Engine — Internal Service API",
        version=__version__,
        lifespan=lifespan,
    )

    # F13: per-route rate limiting. Registered BEFORE the correlation
    # middleware in source order — Starlette's middleware stack treats
    # the last ``add_middleware`` call as the outermost wrapper, so this
    # ordering puts correlation OUTSIDE rate-limit at runtime. The cid is
    # therefore bound by the time the limiter's 429 log line is emitted.
    #
    # D3: ``authenticated_only`` — the limiter runs before routing, while
    # the capability-token gate is a route dependency, and every local
    # client shares the 127.0.0.1 bucket. Without this an unauthenticated
    # localhost page could exhaust the ``/shutdown``, ``/consent/reset``
    # and ``/api/launch`` budgets and starve the real clients; now budget
    # is consumed only after the token validates.
    app.add_middleware(RateLimitMiddleware, authenticated_only=True)

    # F19: correlation IDs. Every request enters a scope that mints (or
    # accepts via ``X-Cortex-Request-ID``) a correlation id, binds it to
    # both ``contextvars`` and structlog, and echoes it back on the
    # response so the calling UI can quote it in error toasts.
    # D16: a supplied id is only honoured when it is bounded and made of
    # id characters; anything else is replaced with a minted id so junk
    # can never be echoed into headers or log lines.
    class _CorrelationMiddleware(BaseHTTPMiddleware):
        async def dispatch(
            self, request: Request, call_next: RequestResponseEndpoint,
        ) -> Response:
            incoming = sanitize_correlation_id(request.headers.get(_REQUEST_ID_HEADER))
            with correlation_scope(incoming) as cid:
                response = await call_next(request)
                response.headers[_REQUEST_ID_HEADER] = cid
                return response

    app.add_middleware(_CorrelationMiddleware)

    # CORS — allow local extensions to connect. Expose the request-id
    # header so browser-side clients can read it off responses.
    # Phase-4b TASK L: the static origin allowlist now lives on
    # APIConfig.cors_allow_origins so deployments can extend it via
    # config rather than patching this file.
    #
    # D12: no ``allow_credentials``. The daemon has no cookie or HTTP-auth
    # session — the capability token travels in an explicitly set
    # ``Authorization`` / ``X-Cortex-Auth-Token`` header, which CORS
    # treats as a plain request header, not a credential — so credentialed
    # cross-origin access to every localhost port bought nothing and
    # widened the reach of any page served from a local dev server.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(getattr(cfg, "cors_allow_origins", []) or [
            "http://localhost",
            "http://127.0.0.1",
        ]),
        allow_origin_regex=(
            r"^(https?://(localhost|127\.0\.0\.1)(:\d+)?"
            r"|chrome-extension://[a-p]{32}"
            r"|moz-extension://[A-Za-z0-9-]+"
            r"|vscode-webview://[A-Za-z0-9-]+)$"
        ),
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[_REQUEST_ID_HEADER],
    )

    # Store config on app state for access in routes
    app.state.config = cfg
    app.state.cortex_config = cortex_config
    app.state.registry = app_services
    app.state.clock = app_clock
    app.state.started_at_mono_ns = app_clock.monotonic_ns()
    app.state.started_boot_id = app_clock.boot_id

    # Register routes — health is mounted without auth so the supervisor
    # liveness probe can reach the daemon before the UI has presented
    # its token; every other route inherits the systemic capability-token
    # gate via ``dependencies=[Depends(require_capability_token)]``
    # (audit Debt-2). A new route added to ``router`` automatically gets
    # the gate; a new route added to ``health_router`` is by-convention
    # liveness-only and visible in code review.
    from cortex.services.api_gateway.routes import (
        health_router,
        metrics_router,
        router,
    )

    # SECURITY (audit Debt-2, revised by D12): only ``/health`` lives on
    # the UNAUTHENTICATED ``health_router`` — it is the liveness/readiness
    # probe the launchers poll before they own a token, and it is DB-free
    # and cheap (D4). ``/metrics`` exposes biometric-derived counters
    # (state transitions, interventions, capture drops), so it now lives
    # on ``metrics_router`` which carries the capability-token dependency
    # itself: a Prometheus scraper on this machine presents the token via
    # ``authorization_credentials`` exactly like every other client. Every
    # other route stays on the gated ``router``.
    app.include_router(health_router)
    app.include_router(metrics_router)
    app.include_router(router, dependencies=[Depends(require_capability_token)])

    logger.info(f"API Gateway configured on {cfg.host}:{cfg.port}")

    return app
