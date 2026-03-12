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

Configuration: APIConfig (host=127.0.0.1, port=9472)
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from cortex.libs.config.settings import APIConfig, CortexConfig

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

    # CORS — allow local extensions to connect
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:*",
            "http://127.0.0.1:*",
            "chrome-extension://*",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Store config on app state for access in routes
    app.state.config = cfg
    app.state.cortex_config = cortex_config
    app.state.registry = registry

    # Register routes
    from cortex.services.api_gateway.routes import router

    app.include_router(router)

    logger.info(f"API Gateway configured on {cfg.host}:{cfg.port}")

    return app
