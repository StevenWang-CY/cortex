"""P1-2: _get_registry null-object fallback.

When ``app.state.registry`` is absent (e.g. a lightweight test rig that
builds the FastAPI app without the real daemon), routes must return
graceful responses instead of crashing with ``AttributeError``.

Asserts:
* ``_get_registry`` returns the ``_EMPTY_REGISTRY`` sentinel when
  ``app.state`` does not have a ``registry`` attribute.
* Hitting registry-dependent endpoints on such an app returns HTTP 200
  (or another non-500 status), not an unhandled exception.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cortex.services.api_gateway.routes import (
    _EMPTY_REGISTRY,
    _NullRegistry,
    _get_registry,
    health_router,
)


# ---------------------------------------------------------------------------
# Unit: _get_registry with no registry on app state
# ---------------------------------------------------------------------------


def test_get_registry_returns_null_sentinel_when_missing() -> None:
    """`_get_registry` returns _EMPTY_REGISTRY when app.state lacks ``registry``."""

    class _FakeState:
        pass

    class _FakeApp:
        state = _FakeState()

    class _FakeRequest:
        app = _FakeApp()

    result = _get_registry(_FakeRequest())  # type: ignore[arg-type]
    assert result is _EMPTY_REGISTRY


def test_null_registry_get_returns_none() -> None:
    reg = _NullRegistry()
    assert reg.get("anything") is None
    assert reg.get("state_engine", "extra_arg") is None


def test_null_registry_registered_services_empty() -> None:
    reg = _NullRegistry()
    assert reg.registered_services == []


def test_null_registry_healthy_false() -> None:
    reg = _NullRegistry()
    assert reg.healthy is False


# ---------------------------------------------------------------------------
# Integration: /health on app with no registry
# ---------------------------------------------------------------------------


@pytest.fixture()
def bare_app() -> FastAPI:
    """FastAPI app with health_router but NO registry on app.state."""
    app = FastAPI()
    app.include_router(health_router)
    # Deliberately omit app.state.registry
    return app


def test_health_endpoint_graceful_without_registry(bare_app: FastAPI) -> None:
    """/health returns 200 (not 500) when no registry is configured."""
    with TestClient(bare_app) as client:
        r = client.get("/health")
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    body = r.json()
    # _NullRegistry.healthy == False → status is "unhealthy", not a crash
    assert body["status"] == "unhealthy"
    assert body["services"] == {}


def test_metrics_endpoint_graceful_without_registry(bare_app: FastAPI) -> None:
    """/metrics returns 200 with text/plain content."""
    with TestClient(bare_app) as client:
        r = client.get("/metrics")
    assert r.status_code == 200
    ct = r.headers.get("content-type", "")
    assert "text/plain" in ct
