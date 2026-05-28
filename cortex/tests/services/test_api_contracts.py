"""Phase 4.4 — API contract regression tests.

Covers the four schema-layer fixes:

- **T1**: every response-model ``timestamp`` field defaults to wall-clock
  seconds (``time.time``), not process-relative ``time.monotonic``.
- **T2**: ``GET /api/cost`` exists, returns a 200 with the documented
  shape, and degrades gracefully to a zero-valued ``provider="none"``
  response when no cost tracker is registered.
- **T3**: ``StatusResponse`` carries an explicit
  ``status: Literal["initializing","ready","degraded"]`` discriminator
  and the route stamps it.
- **T4**: ``POST /consent/reset`` accepts an explicit
  ``ConsentResetRequest`` body without breaking the no-body path.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cortex.services.api_gateway.app import create_app, registry
from cortex.services.api_gateway.routes import (
    AckResponse,
    ConsentLevelResponse,
    ConsentResetRequest,
    ConsentResetResponse,
    ContextBuildResponse,
    CostResponse,
    DashboardRaiseResponse,
    HelpfulnessSummaryResponse,
    InterventionApplyResponse,
    InterventionRestoreResponse,
    LLMPlanResponse,
    ShutdownResponse,
    StateInferResponse,
    StatusResponse,
    StressIntegralResponse,
)

# ─── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_registry() -> Any:
    """Isolate the global registry between tests."""
    registry.reset()
    yield
    registry.reset()


@pytest.fixture()
def client(tmp_path: Any, monkeypatch: Any) -> TestClient:
    """Authenticated FastAPI test client (mirrors ``test_api_gateway``)."""
    from cortex.libs.auth.local_token import load_or_create_token

    token_file = tmp_path / "auth.token"
    monkeypatch.setattr(
        "cortex.libs.auth.local_token.auth_token_path",
        lambda: token_file,
    )
    token = load_or_create_token(token_file)
    app = create_app()
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {token}"})
        yield c


# ─── T1: wall-clock timestamps ────────────────────────────────────────


class TestWallClockTimestamps:
    """Every response-model ``timestamp`` default factory uses
    ``time.time``, not ``time.monotonic``. The acceptance check is
    ``abs(model.timestamp - time.time()) < 5`` immediately after
    construction — a monotonic-relative default would be far outside
    that band on any process that has been alive for more than five
    seconds.
    """

    @pytest.mark.parametrize(
        "model_cls",
        [
            AckResponse,
            ShutdownResponse,
            DashboardRaiseResponse,
            StatusResponse,
            ContextBuildResponse,
            LLMPlanResponse,
            InterventionApplyResponse,
            InterventionRestoreResponse,
            StressIntegralResponse,
            HelpfulnessSummaryResponse,
            ConsentLevelResponse,
            ConsentResetResponse,
        ],
    )
    def test_timestamp_is_wall_clock(self, model_cls: type) -> None:
        """The default ``timestamp`` is within 5 s of ``time.time()``."""
        before = time.time()
        # ``StateInferResponse`` and a few other models have required
        # fields; for the ones that don't, construct with defaults.
        instance = model_cls()
        after = time.time()
        assert before - 5.0 <= instance.timestamp <= after + 5.0, (
            f"{model_cls.__name__}.timestamp={instance.timestamp} is "
            f"not within 5 s of wall-clock [{before}, {after}] — likely "
            "a regression to time.monotonic()."
        )

    def test_cost_response_timestamp_is_wall_clock(self) -> None:
        """``CostResponse`` has required ``cost_today``/``budget_today``
        fields; supply zero values to exercise the default timestamp."""
        before = time.time()
        instance = CostResponse(cost_today=0.0, budget_today=0.0)
        after = time.time()
        assert before - 5.0 <= instance.timestamp <= after + 5.0

    def test_state_infer_response_timestamp_is_wall_clock(self) -> None:
        """``StateInferResponse`` has a required ``estimate`` field, so
        it gets a dedicated test that supplies a minimal estimate."""
        from cortex.libs.schemas.state import (
            SignalQuality,
            StateEstimate,
            StateScores,
        )

        estimate = StateEstimate(
            state="FLOW",
            confidence=0.5,
            scores=StateScores(flow=0.5, hypo=0.0, hyper=0.0, recovery=0.0),
            reasons=[],
            signal_quality=SignalQuality(physio=0.8, kinematics=0.7, telemetry=0.9),
            timestamp=time.time(),
            dwell_seconds=0.0,
        )
        before = time.time()
        resp = StateInferResponse(estimate=estimate)
        after = time.time()
        assert before - 5.0 <= resp.timestamp <= after + 5.0


# ─── T2: /api/cost endpoint ───────────────────────────────────────────


class _FakeCostTracker:
    """Mimics the subset of :class:`CostTracker` the route reads.

    ``today_total_usd`` is the only mandatory method; the optional
    token-counter attributes are exposed to verify the route surfaces
    them when present.
    """

    def __init__(
        self,
        *,
        today_usd: float = 1.23,
        session_baseline: float = 0.0,
        prompt_tokens: int = 4096,
        completion_tokens: int = 512,
    ) -> None:
        self._today_usd = today_usd
        self.session_start_total_usd = session_baseline
        self.prompt_tokens_today = prompt_tokens
        self.completion_tokens_today = completion_tokens

    def today_total_usd(self) -> float:
        return self._today_usd

    def check_budget(self) -> str:
        return "OK"


class _FakeLLMClient:
    """Stand-in for the daemon's LLM client.

    The route prefers a directly-registered ``cost_tracker``; when one
    isn't present it falls back to ``llm_client._cost_tracker``. This
    fake exercises the fallback path.
    """

    def __init__(self, tracker: _FakeCostTracker, model: str = "claude-sonnet-4-5") -> None:
        self._cost_tracker = tracker
        self.model = model


class _FakeLLMConfig:
    provider = "anthropic_direct"


class _FakeConfig:
    llm = _FakeLLMConfig()


class _FakeDaemon:
    """Provides ``daemon.config.llm.provider`` so the route can echo
    the active provider in the response."""

    config = _FakeConfig()


class TestCostEndpoint:
    """``GET /api/cost`` surface — see :func:`get_cost`."""

    def test_returns_zeroed_response_when_no_tracker(
        self, client: TestClient,
    ) -> None:
        """When no tracker is registered, the route returns a 200 with a
        zero-valued ``CostResponse`` and ``provider=None`` — not a 404.
        The shell polls unconditionally. The canonical wire shape is
        ``cost_today``/``budget_today``/``budget_exhausted``; the popup
        and the generated TS schema consume these field names."""
        resp = client.get("/api/cost")
        assert resp.status_code == 200
        body = resp.json()
        assert body["cost_today"] == 0.0
        assert body["budget_today"] == 0.0
        assert body["budget_exhausted"] is False
        assert body["provider"] is None
        # T1: timestamp is wall-clock.
        assert abs(body["timestamp"] - time.time()) < 5.0

    def test_returns_tracker_values_when_registered_directly(
        self, client: TestClient,
    ) -> None:
        """Registering a tracker under ``cost_tracker`` short-circuits
        the LLM-client lookup."""
        tracker = _FakeCostTracker(
            today_usd=2.50,
            session_baseline=0.50,
            prompt_tokens=1000,
            completion_tokens=250,
        )
        registry.register("cost_tracker", tracker)
        registry.register("daemon", _FakeDaemon())

        resp = client.get("/api/cost")
        assert resp.status_code == 200
        body = resp.json()
        assert body["cost_today"] == pytest.approx(2.50)
        assert body["prompt_tokens"] == 1000
        assert body["completion_tokens"] == 250
        assert body["provider"] == "anthropic_direct"

    def test_falls_back_to_llm_client_private_tracker(
        self, client: TestClient,
    ) -> None:
        """When no direct ``cost_tracker`` is registered, the route
        peeks at ``llm_client._cost_tracker`` (the planner-attached
        attribute set in :mod:`anthropic_planner`)."""
        tracker = _FakeCostTracker(today_usd=0.42)
        registry.register("llm_client", _FakeLLMClient(tracker))
        registry.register("daemon", _FakeDaemon())

        resp = client.get("/api/cost")
        assert resp.status_code == 200
        body = resp.json()
        assert body["cost_today"] == pytest.approx(0.42)
        assert body["model"] == "claude-sonnet-4-5"
        assert body["provider"] == "anthropic_direct"

    def test_response_model_validates_shape(self) -> None:
        """The response model itself rejects negative values and
        accepts the documented field set."""
        # Negative cost is rejected.
        with pytest.raises(Exception):
            CostResponse(cost_today=-1.0, budget_today=0.0)

        # The documented shape parses cleanly.
        resp = CostResponse(
            cost_today=1.0,
            budget_today=5.0,
            prompt_tokens=10,
            completion_tokens=5,
            provider="bedrock",
            model="claude-sonnet-4-5",
        )
        assert resp.provider == "bedrock"


# ─── T3: StatusResponse discriminator ─────────────────────────────────


class TestStatusDiscriminator:
    """``StatusResponse`` carries an explicit ``status`` discriminator
    so clients can branch without inspecting the nullability of
    ``state``/``features``."""

    def test_default_status_is_initializing(self) -> None:
        resp = StatusResponse()
        assert resp.status == "initializing"

    def test_route_stamps_ready_when_estimate_present(
        self, client: TestClient,
    ) -> None:
        """When the registry has a ``latest_state_estimate``, the
        route should stamp ``status="ready"``."""
        from cortex.libs.schemas.state import (
            SignalQuality,
            StateEstimate,
            StateScores,
        )

        estimate = StateEstimate(
            state="FLOW",
            confidence=0.75,
            scores=StateScores(flow=0.75, hypo=0.0, hyper=0.0, recovery=0.0),
            reasons=["stable"],
            signal_quality=SignalQuality(physio=0.9, kinematics=0.8, telemetry=0.9),
            timestamp=time.time(),
            dwell_seconds=10.0,
        )
        registry.register("latest_state_estimate", estimate)

        resp = client.get("/status/current")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ready"
        assert body["state"] == "FLOW"

    def test_route_stamps_initializing_when_empty(
        self, client: TestClient,
    ) -> None:
        resp = client.get("/status/current")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "initializing"


# ─── T4: ConsentResetRequest ──────────────────────────────────────────


class TestConsentResetRequest:
    """``POST /consent/reset`` accepts an explicit (currently-empty)
    request body without breaking the no-body path."""

    def test_request_model_constructs_with_no_fields(self) -> None:
        # Plain ``ConsentResetRequest()`` is valid — the model is
        # intentionally empty so future fields can be added optionally.
        req = ConsentResetRequest()
        assert req.model_dump() == {}

    def test_route_accepts_empty_body(self, client: TestClient) -> None:
        resp = client.post("/consent/reset")
        assert resp.status_code == 200
        body = resp.json()
        assert "reset" in body
        assert "levels" in body
        # T1: timestamp is wall-clock.
        assert abs(body["timestamp"] - time.time()) < 5.0

    def test_route_accepts_explicit_empty_json(
        self, client: TestClient,
    ) -> None:
        resp = client.post("/consent/reset", json={})
        assert resp.status_code == 200
