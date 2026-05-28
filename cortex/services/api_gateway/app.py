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
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute
from starlette.middleware.base import BaseHTTPMiddleware

from cortex.libs.config.settings import APIConfig, CortexConfig
from cortex.libs.logging.correlation import correlation_scope
from cortex.services.api_gateway.auth import require_capability_token
from cortex.services.api_gateway.middleware.rate_limit import RateLimitMiddleware

_REQUEST_ID_HEADER = "X-Cortex-Request-ID"

logger = logging.getLogger(__name__)


class ServiceRegistry:
    """
    Registry for service instances used by API endpoints.

    Holds references to engines and services that can be injected
    into route handlers. Services are registered during app startup.
    """

    def __init__(self) -> None:
        self._services: dict[str, Any] = {}
        self._healthy: bool = False

    def register(self, name: str, service: Any) -> None:
        """Register a service by name."""
        self._services[name] = service
        logger.info(f"Registered service: {name}")

    def get(self, name: str) -> Any | None:
        """Get a registered service by name."""
        return self._services.get(name)

    def has(self, name: str) -> bool:
        """Check if a service is registered."""
        return name in self._services

    @property
    def registered_services(self) -> list[str]:
        """List all registered service names."""
        return list(self._services.keys())

    @property
    def healthy(self) -> bool:
        return self._healthy

    @healthy.setter
    def healthy(self, value: bool) -> None:
        self._healthy = value

    def reset(self) -> None:
        """Clear all registered services."""
        self._services.clear()
        self._healthy = False


# Global service registry (singleton)
registry = ServiceRegistry()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan handler.

    Startup: mark system healthy, log readiness.
    Shutdown: mark unhealthy, clean up services.
    """
    logger.info("Cortex API Gateway starting up")
    registry.healthy = True
    logger.info(
        f"Services registered: {registry.registered_services}"
    )
    yield
    logger.info("Cortex API Gateway shutting down")
    registry.healthy = False


def create_app(
    config: APIConfig | None = None,
    cortex_config: CortexConfig | None = None,
) -> FastAPI:
    """
    Create and configure the FastAPI application.

    Args:
        config: API configuration. Defaults to APIConfig().
        cortex_config: Full Cortex configuration for service initialization.

    Returns:
        Configured FastAPI application.
    """
    cfg = config or APIConfig()

    app = FastAPI(
        title="Cortex API Gateway",
        description="Somatic Workspace Engine — Internal Service API",
        version="0.1.0",
        lifespan=lifespan,
    )

    # F13: per-route rate limiting. Registered BEFORE the correlation
    # middleware in source order — Starlette's middleware stack treats
    # the last ``add_middleware`` call as the outermost wrapper, so this
    # ordering puts correlation OUTSIDE rate-limit at runtime. The cid is
    # therefore bound by the time the limiter's 429 log line is emitted.
    app.add_middleware(RateLimitMiddleware)

    # F19: correlation IDs. Every request enters a scope that mints (or
    # accepts via ``X-Cortex-Request-ID``) a correlation id, binds it to
    # both ``contextvars`` and structlog, and echoes it back on the
    # response so the calling UI can quote it in error toasts.
    class _CorrelationMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            incoming = request.headers.get(_REQUEST_ID_HEADER)
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
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[_REQUEST_ID_HEADER],
    )

    # Store config on app state for access in routes
    app.state.config = cfg
    app.state.cortex_config = cortex_config
    app.state.registry = registry

    # Register routes — health is mounted without auth so the supervisor
    # liveness probe can reach the daemon before the UI has presented
    # its token; every other route inherits the systemic capability-token
    # gate via ``dependencies=[Depends(require_capability_token)]``
    # (audit Debt-2). A new route added to ``router`` automatically gets
    # the gate; a new route added to ``health_router`` is by-convention
    # liveness-only and visible in code review.
    from cortex.services.api_gateway.routes import (
        health_router,
        prometheus_metrics,
        router,
    )

    # SECURITY (audit Debt-2): ``/metrics`` exposes the full Prometheus
    # registry which includes labelled state-transition counters and
    # daemon uptime — useful for monitoring but also useful for a
    # localhost web page fingerprinting the daemon when it has no auth
    # gate. ``/health`` MUST stay un-authenticated (it's the launcher's
    # liveness probe and runs before the UI has a capability token),
    # so we mount the unauthenticated routes through a fresh router
    # that excludes ``/metrics``, and mount ``/metrics`` on a dedicated
    # router behind the same capability-token gate the rest of
    # ``router`` uses.
    #
    # NOTE: we deliberately do NOT mutate ``health_router.routes`` —
    # that would corrupt the global singleton for tests that import it
    # directly. Instead we copy the non-/metrics routes into a fresh
    # ``unauthenticated_router`` and leave the source ``health_router``
    # untouched.
    unauthenticated_router = APIRouter()
    for r in health_router.routes:
        if isinstance(r, APIRoute) and r.path != "/metrics":
            unauthenticated_router.routes.append(r)
    metrics_router = APIRouter()
    metrics_router.add_api_route(
        "/metrics",
        prometheus_metrics,
        methods=["GET"],
    )

    app.include_router(unauthenticated_router)
    app.include_router(
        metrics_router,
        dependencies=[Depends(require_capability_token)],
    )
    app.include_router(router, dependencies=[Depends(require_capability_token)])

    logger.info(f"API Gateway configured on {cfg.host}:{cfg.port}")

    return app
