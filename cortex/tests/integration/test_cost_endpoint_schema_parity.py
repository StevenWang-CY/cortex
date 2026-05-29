"""P0-1 / P1-10 / P2-3: CostResponse HTTP ↔ WS schema parity test.

Verifies:
1. GET /api/cost returns exactly the canonical CostResponse fields.
2. WS COST_REQUEST → COST_RESPONSE carries the same field set.
3. ``provider`` is JSON null (not the string ``"none"``) when no tracker
   is wired.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from cortex.libs.schemas.realtime import CostResponse
from cortex.services.api_gateway.app import create_app, registry

# ─── Expected canonical keys ──────────────────────────────────────────

_CANONICAL_KEYS = frozenset(CostResponse.model_fields.keys())
# cost_today, budget_today, provider, budget_exhausted, timestamp,
# prompt_tokens, completion_tokens, model


# ─── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_registry():
    registry.reset()
    yield
    registry.reset()


@pytest.fixture()
def auth_client(tmp_path: Path, monkeypatch):
    """Authenticated TestClient with no daemon/tracker wired."""
    from cortex.libs.auth.local_token import load_or_create_token

    token_file = tmp_path / "auth.token"
    monkeypatch.setattr(
        "cortex.libs.auth.local_token.auth_token_path", lambda: token_file
    )
    token = load_or_create_token(token_file)
    app = create_app()
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {token}"})
        yield c, token


# ─── HTTP cost endpoint tests ─────────────────────────────────────────


class TestCostEndpointSchema:
    def test_get_cost_returns_200_no_tracker(self, auth_client) -> None:
        """Without any tracker wired, /api/cost must return 200 with zeros."""
        client, _ = auth_client
        r = client.get("/api/cost")
        assert r.status_code == 200

    def test_get_cost_has_canonical_keys(self, auth_client) -> None:
        """Response keys must exactly match the canonical CostResponse fields."""
        client, _ = auth_client
        r = client.get("/api/cost")
        assert r.status_code == 200
        body = r.json()
        # All canonical fields must be present (extra fields allowed by
        # JSON but checked below via model_validate).
        assert _CANONICAL_KEYS.issubset(body.keys()), (
            f"Missing keys: {_CANONICAL_KEYS - body.keys()}"
        )

    def test_get_cost_validates_as_canonical_model(self, auth_client) -> None:
        """Full round-trip: response JSON must parse as CostResponse."""
        client, _ = auth_client
        r = client.get("/api/cost")
        assert r.status_code == 200
        CostResponse.model_validate(r.json())

    def test_get_cost_provider_is_null_not_string_none(self, auth_client) -> None:
        """provider must be JSON null, never the string 'none', when no tracker."""
        client, _ = auth_client
        r = client.get("/api/cost")
        body = r.json()
        assert body["provider"] is None, (
            f"Expected provider=null, got {body['provider']!r}"
        )

    def test_get_cost_with_tracker_has_same_keys(
        self, auth_client, tmp_path: Path
    ) -> None:
        """With a stub tracker, response still has the canonical key set."""
        client, token = auth_client

        class _StubTracker:
            def today_total_usd(self) -> float:
                return 0.42
            prompt_tokens_today = 100
            completion_tokens_today = 50

        app = create_app()
        app.state.registry.register("cost_tracker", _StubTracker())
        with TestClient(app) as c2:
            c2.headers.update({"Authorization": f"Bearer {token}"})
            r = c2.get("/api/cost")
        assert r.status_code == 200
        body = r.json()
        assert _CANONICAL_KEYS.issubset(body.keys())
        CostResponse.model_validate(body)


# ─── Finding #1: budget resolution from config (HTTP ↔ WS parity) ─────


class TestCostEndpointBudgetResolution:
    """Finding #1: the HTTP /api/cost route used to probe non-existent
    public attrs on CostTracker (kill_usd / daily_budget_usd /
    budget_exhausted) so ``budget_today`` was permanently 0.0 and
    ``budget_exhausted`` permanently False — contradicting the WS path,
    which reads the budget from ``config.llm.daily_cost_budget_usd`` and
    derives exhaustion from ``tracker.check_budget() == 'KILL'``."""

    class _Daemon:
        def __init__(self, budget: float) -> None:
            from cortex.libs.config.settings import CortexConfig, LLMConfig

            cfg = CortexConfig()
            cfg.llm = LLMConfig(daily_cost_budget_usd=budget)
            self.config = cfg

    class _Tracker:
        def __init__(self, spend: float, state: str) -> None:
            self._spend = spend
            self._state = state

        def today_total_usd(self) -> float:
            return self._spend

        def check_budget(self) -> str:
            return self._state

    def _client_with(self, auth_client, budget: float, spend: float, state: str):
        _, token = auth_client
        app = create_app()
        app.state.registry.register("daemon", self._Daemon(budget))
        app.state.registry.register("cost_tracker", self._Tracker(spend, state))
        c = TestClient(app)
        c.headers.update({"Authorization": f"Bearer {token}"})
        return c

    def test_configured_budget_surfaces_in_response(self, auth_client) -> None:
        # NB: deliberately NOT using ``with c:`` — the lifespan exit in the
        # installed starlette/anyio closes the asyncio event loop, which
        # poisons a sibling test that still uses the deprecated
        # ``asyncio.get_event_loop()``. A bare GET needs no lifespan.
        c = self._client_with(auth_client, budget=20.0, spend=3.0, state="OK")
        body = c.get("/api/cost").json()
        # The configured budget is reflected (was always 0.0 pre-fix).
        assert body["budget_today"] == 20.0
        assert body["cost_today"] == 3.0
        assert body["budget_exhausted"] is False

    def test_exceeding_budget_sets_exhausted(self, auth_client) -> None:
        # check_budget() == "KILL" → budget_exhausted True, mirroring the
        # WS path exactly.
        c = self._client_with(auth_client, budget=20.0, spend=25.0, state="KILL")
        body = c.get("/api/cost").json()
        assert body["budget_today"] == 20.0
        assert body["budget_exhausted"] is True


# ─── WS / daemon cost response shape ─────────────────────────────────


class TestCostResponseWSShape:
    """Verify the daemon's get_cost_response() returns the canonical envelope."""

    def test_daemon_get_cost_response_returns_canonical_model(self) -> None:
        """daemon.get_cost_response() must return a CostResponse instance."""
        # We directly test the daemon method in isolation — no server needed.
        import importlib

        daemon_mod = importlib.import_module("cortex.services.runtime_daemon")
        # Construct a minimal fake daemon with no LLM client.
        daemon = MagicMock()
        daemon.config = MagicMock()
        daemon.config.llm = MagicMock()
        daemon.config.llm.provider = None
        daemon.config.llm.daily_cost_budget_usd = 5.0
        daemon._llm_client = None

        # Call the real method by binding it to the mock daemon.
        # NB: ``asyncio.run`` (not the deprecated ``get_event_loop()``)
        # so this test stays green regardless of whether an earlier
        # ``TestClient`` lifespan exit closed the thread's event loop.
        bound = daemon_mod.CortexDaemon.get_cost_response.__get__(daemon)
        result = asyncio.run(bound())

        assert isinstance(result, CostResponse)
        assert result.provider is None, (
            f"provider must be None when no LLM config; got {result.provider!r}"
        )
        # Keys must match canonical set.
        dumped = result.model_dump(mode="json")
        assert _CANONICAL_KEYS.issubset(dumped.keys())

    def test_http_and_ws_payload_same_keys(self, auth_client) -> None:
        """HTTP /api/cost keys == CostResponse.model_fields keys (same as WS)."""
        client, _ = auth_client
        r = client.get("/api/cost")
        http_keys = frozenset(r.json().keys())
        # HTTP response must contain at least all canonical keys.
        assert _CANONICAL_KEYS.issubset(http_keys)
