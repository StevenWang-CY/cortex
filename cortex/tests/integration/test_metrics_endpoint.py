"""P1-19: /metrics endpoint integration test.

Asserts:
* GET /metrics returns HTTP 200.
* Content-Type is text/plain (Prometheus exposition format).
* The response body contains the uptime gauge
  (``cortex_daemon_uptime_seconds``).
* The response body contains the coalesce-drops counter
  (``cortex_ws_coalesce_drops_total``).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cortex.services.api_gateway.routes import health_router


@pytest.fixture()
def metrics_app() -> FastAPI:
    """Minimal FastAPI app with only the health_router mounted."""
    app = FastAPI()
    app.include_router(health_router)
    return app


def test_metrics_returns_200(metrics_app: FastAPI) -> None:
    with TestClient(metrics_app) as client:
        r = client.get("/metrics")
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text[:200]}"


def test_metrics_content_type_is_text_plain(metrics_app: FastAPI) -> None:
    with TestClient(metrics_app) as client:
        r = client.get("/metrics")
    ct = r.headers.get("content-type", "")
    assert "text/plain" in ct, f"Expected text/plain content-type, got: {ct}"


def test_metrics_contains_uptime_gauge(metrics_app: FastAPI) -> None:
    """cortex_daemon_uptime_seconds must appear in the output."""
    with TestClient(metrics_app) as client:
        r = client.get("/metrics")
    assert "cortex_daemon_uptime_seconds" in r.text, (
        "cortex_daemon_uptime_seconds gauge not found in /metrics output"
    )


def test_metrics_contains_coalesce_drops_counter(metrics_app: FastAPI) -> None:
    """cortex_ws_coalesce_drops_total must appear in the output."""
    with TestClient(metrics_app) as client:
        r = client.get("/metrics")
    assert "cortex_ws_coalesce_drops_total" in r.text, (
        "cortex_ws_coalesce_drops_total counter not found in /metrics output"
    )


def test_metrics_contains_state_transitions_counter(metrics_app: FastAPI) -> None:
    """cortex_state_transitions_total must appear in the output."""
    with TestClient(metrics_app) as client:
        r = client.get("/metrics")
    assert "cortex_state_transitions_total" in r.text, (
        "cortex_state_transitions_total counter not found in /metrics output"
    )


def test_metrics_contains_interventions_applied_counter(metrics_app: FastAPI) -> None:
    """cortex_interventions_applied_total must appear in the output."""
    with TestClient(metrics_app) as client:
        r = client.get("/metrics")
    assert "cortex_interventions_applied_total" in r.text, (
        "cortex_interventions_applied_total counter not found in /metrics output"
    )
