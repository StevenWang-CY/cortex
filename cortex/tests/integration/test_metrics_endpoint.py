"""P1-19 / D12: ``/metrics`` endpoint integration test.

Asserts:
* GET /metrics without a capability token returns 401 (D12 — the
  exposition contains biometric-derived counters such as state
  transitions and interventions applied, so it is no longer readable by
  any localhost origin).
* GET /metrics with the token returns HTTP 200 in the Prometheus text
  exposition format and contains the guaranteed metrics
  (``cortex_daemon_uptime_seconds``, ``cortex_ws_coalesce_drops_total``,
  ``cortex_state_transitions_total``,
  ``cortex_interventions_applied_total``).
* The gate is carried by ``metrics_router`` itself, so it holds even
  when the router is mounted without ``create_app``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cortex.libs.auth.local_token import load_or_create_token
from cortex.services.api_gateway.routes import metrics_router


@pytest.fixture()
def auth_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Provision a capability token in ``tmp_path`` and return its value."""
    token_file = tmp_path / "auth.token"
    monkeypatch.setattr("cortex.libs.auth.local_token.auth_token_path", lambda: token_file)
    return load_or_create_token(token_file)


@pytest.fixture()
def metrics_app(auth_token: str) -> FastAPI:
    """Minimal FastAPI app with only ``metrics_router`` mounted (no extra deps)."""
    app = FastAPI()
    app.include_router(metrics_router)
    return app


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_metrics_requires_token_through_full_app(auth_token: str) -> None:
    """D12: through the REAL ``create_app()`` wiring ``GET /metrics`` is
    401 without a token and 200 with it; ``/health`` stays open."""
    from cortex.services.api_gateway.app import create_app, registry

    registry.reset()
    app = create_app()
    try:
        with TestClient(app) as client:
            tokenless = client.get("/metrics")
            wrong = client.get("/metrics", headers=_auth("0" * 64))
            ok = client.get("/metrics", headers=_auth(auth_token))
            health = client.get("/health")
        assert tokenless.status_code == 401, tokenless.text[:200]
        assert "cortex_" not in tokenless.text
        assert wrong.status_code == 401, wrong.text[:200]
        assert ok.status_code == 200, ok.text[:200]
        assert "text/plain" in ok.headers.get("content-type", "")
        assert "cortex_daemon_uptime_seconds" in ok.text
        assert health.status_code == 200
    finally:
        registry.reset()


def test_metrics_router_carries_the_gate_itself(metrics_app: FastAPI) -> None:
    """The dependency lives on ``metrics_router``, not on the mount call."""
    with TestClient(metrics_app) as client:
        r = client.get("/metrics")
    assert r.status_code == 401, f"Expected 401, got {r.status_code}: {r.text[:200]}"


def test_metrics_returns_200(metrics_app: FastAPI, auth_token: str) -> None:
    with TestClient(metrics_app) as client:
        r = client.get("/metrics", headers=_auth(auth_token))
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text[:200]}"


def test_metrics_content_type_is_text_plain(metrics_app: FastAPI, auth_token: str) -> None:
    with TestClient(metrics_app) as client:
        r = client.get("/metrics", headers=_auth(auth_token))
    ct = r.headers.get("content-type", "")
    assert "text/plain" in ct, f"Expected text/plain content-type, got: {ct}"


@pytest.mark.parametrize(
    "metric",
    [
        "cortex_daemon_uptime_seconds",
        "cortex_ws_coalesce_drops_total",
        "cortex_state_transitions_total",
        "cortex_interventions_applied_total",
    ],
)
def test_metrics_contains_guaranteed_metric(
    metrics_app: FastAPI, auth_token: str, metric: str
) -> None:
    with TestClient(metrics_app) as client:
        r = client.get("/metrics", headers=_auth(auth_token))
    assert metric in r.text, f"{metric} not found in /metrics output"
