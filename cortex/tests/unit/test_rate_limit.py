"""Audit F13 — per-route rate limiting on the API gateway.

The pre-fix gateway accepted unbounded ``/state/infer``, ``/llm/plan``,
``/apply_intervention``, and ``/shutdown`` traffic from any localhost
client. A tight-loop browser tab or buggy extension could:

* OOM the daemon via the per-call numpy allocations on ``/state/infer``;
* rack up Anthropic spend via ``/llm/plan``;
* SIGTERM-storm the daemon via ``/shutdown``.

The fix is a tiny per-IP token bucket per route plus a ``Retry-After``
envelope on 429. These tests pin the behaviour. They each fail on
``main`` because:

* the module ``cortex.services.api_gateway.middleware.rate_limit`` does
  not exist, so import-time ``ImportError`` short-circuits every test;
* even if the import were stubbed, ``/state/infer`` returns 200 for the
  Nth call past the cap (no 429 ever emitted).
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cortex.libs.logging.structured import EventType
from cortex.services.api_gateway.middleware.rate_limit import (
    DEFAULT_LIMITS,
    RateLimitMiddleware,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_app(*, limits: dict[str, int] | None = None) -> tuple[FastAPI, RateLimitMiddleware]:
    """Build a minimal FastAPI app that wires only the rate limiter.

    Routes are no-op echoes — the actual handler logic is out of scope
    for the rate-limit unit; the middleware decision precedes the route.
    """
    app = FastAPI()

    # Hand-roll the middleware so the test can introspect the instance.
    # ``app.add_middleware`` constructs lazily inside ``build_middleware_stack``;
    # we want the same instance every call. Subclass to capture it.
    captured: dict[str, RateLimitMiddleware] = {}

    class _CapturingMiddleware(RateLimitMiddleware):
        def __init__(self, app, **kw):  # type: ignore[no-untyped-def]
            super().__init__(app, **kw)
            captured["m"] = self

    app.add_middleware(_CapturingMiddleware, limits=limits)

    @app.post("/state/infer")
    async def _state_infer() -> dict:  # pragma: no cover - exercised by tests
        return {"ok": True}

    @app.post("/llm/plan")
    async def _llm_plan() -> dict:  # pragma: no cover
        return {"ok": True}

    @app.post("/intervention/apply")
    async def _apply_intervention() -> dict:  # pragma: no cover
        return {"ok": True}

    @app.post("/shutdown")
    async def _shutdown() -> dict:  # pragma: no cover
        return {"ok": True}

    @app.get("/health")
    async def _health() -> dict:  # pragma: no cover
        return {"ok": True}

    # Force the middleware stack to build so ``captured["m"]`` is populated.
    with TestClient(app):
        pass
    return app, captured["m"]


# ---------------------------------------------------------------------------
# 1. Under-limit accepted
# ---------------------------------------------------------------------------


def test_under_limit_requests_are_accepted() -> None:
    app, _ = _build_app(limits={"/state/infer": 3})
    with TestClient(app) as client:
        for _ in range(3):
            resp = client.post("/state/infer", json={})
            assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# 2. Over-limit returns 429 with Retry-After
# ---------------------------------------------------------------------------


def test_over_limit_returns_429_with_retry_after() -> None:
    app, _ = _build_app(limits={"/state/infer": 2})
    with TestClient(app) as client:
        for _ in range(2):
            client.post("/state/infer", json={})
        resp = client.post("/state/infer", json={})
    assert resp.status_code == 429
    # ``Retry-After`` is the contract the wire commits to; without it the
    # extension cannot back off correctly.
    assert "Retry-After" in resp.headers
    retry_after = int(resp.headers["Retry-After"])
    assert retry_after >= 1
    body = resp.json()
    assert body["error"] == "rate_limited"
    assert body["route"] == "/state/infer"


# ---------------------------------------------------------------------------
# 3. Per-route independence: hammering one route doesn't bounce another
# ---------------------------------------------------------------------------


def test_per_route_limits_are_independent() -> None:
    app, _ = _build_app(limits={"/state/infer": 1, "/llm/plan": 5})
    with TestClient(app) as client:
        # Exhaust /state/infer.
        assert client.post("/state/infer", json={}).status_code == 200
        assert client.post("/state/infer", json={}).status_code == 429
        # /llm/plan must still have all 5 slots.
        for _ in range(5):
            resp = client.post("/llm/plan", json={})
            assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# 4. Window slide: requests fall out after the window expires
# ---------------------------------------------------------------------------


def test_window_slide_frees_slots() -> None:
    """Drive the limiter with a controllable clock and assert recovery."""

    fake_now = {"t": 1000.0}

    def clock() -> float:
        return fake_now["t"]

    app = FastAPI()

    class _ClockMiddleware(RateLimitMiddleware):
        def __init__(self, app, **kw):  # type: ignore[no-untyped-def]
            kw.setdefault("limits", {"/state/infer": 2})
            kw.setdefault("window_seconds", 10.0)
            kw.setdefault("time_func", clock)
            super().__init__(app, **kw)

    app.add_middleware(_ClockMiddleware)

    @app.post("/state/infer")
    async def _state_infer() -> dict:
        return {"ok": True}

    with TestClient(app) as client:
        assert client.post("/state/infer", json={}).status_code == 200
        assert client.post("/state/infer", json={}).status_code == 200
        # Third call is over cap.
        assert client.post("/state/infer", json={}).status_code == 429
        # Advance past the sliding window.
        fake_now["t"] = 1011.0
        # Both previous entries have aged out — call goes through.
        assert client.post("/state/infer", json={}).status_code == 200


# ---------------------------------------------------------------------------
# 5. Correlation id appears in the 429 log line
# ---------------------------------------------------------------------------


def test_cid_included_in_log_line(caplog: pytest.LogCaptureFixture) -> None:
    # Use the full ``create_app`` pipeline so the correlation middleware
    # is wired exactly as in production. The wiring contract under test
    # is: correlation wraps rate-limit, so the cid is bound by the time
    # the 429 log line is emitted.
    from cortex.services.api_gateway.app import create_app, registry

    registry.reset()
    app = create_app()
    # Tighten the limit on the live middleware instance so the test
    # exhausts it quickly without DOS-ing the suite.
    for mw in app.user_middleware:
        # ``Middleware`` is a NamedTuple-like; the class is at index 0.
        cls = mw.cls if hasattr(mw, "cls") else mw[0]
        if cls is RateLimitMiddleware:
            # We have to rebuild with a new limits arg; FastAPI is happy to
            # accept ``add_middleware`` kwargs at registration, so we
            # rewrite the kwargs dict in place before the stack is built.
            kwargs = mw.kwargs if hasattr(mw, "kwargs") else mw[1]
            kwargs["limits"] = {"/state/infer": 1}
            break

    with TestClient(app) as client:
        client.post(
            "/state/infer",
            json={"feature_vector": {}, "signal_quality": {}},
        )
        with caplog.at_level(logging.WARNING):
            resp = client.post(
                "/state/infer",
                json={"feature_vector": {}, "signal_quality": {}},
                headers={"X-Cortex-Request-ID": "cid_testabc01"},
            )
    assert resp.status_code == 429
    # The log line uses the structured EventType value so log aggregators
    # can filter on a stable token.
    matching = [
        rec
        for rec in caplog.records
        if EventType.RATE_LIMITED.value in rec.getMessage()
    ]
    assert matching, "expected a RATE_LIMITED log line"
    msg = matching[0].getMessage()
    assert "cid=cid_testabc01" in msg
    assert "route=/state/infer" in msg
    # The body itself echoes the cid back so the UI toast can quote it.
    assert resp.json()["correlation_id"] == "cid_testabc01"


# ---------------------------------------------------------------------------
# 6. Retry-After is present and >= 1
# ---------------------------------------------------------------------------


def test_retry_after_header_is_present_and_positive() -> None:
    app, _ = _build_app(limits={"/shutdown": 1})
    with TestClient(app) as client:
        assert client.post("/shutdown", json={}).status_code == 200
        bounced = client.post("/shutdown", json={})
    assert bounced.status_code == 429
    assert bounced.headers.get("Retry-After") is not None
    assert int(bounced.headers["Retry-After"]) >= 1


# ---------------------------------------------------------------------------
# 7. Defaults match the cited audit text
# ---------------------------------------------------------------------------


def test_default_limits_match_audit_table() -> None:
    """Defence-in-depth: the limits ship preconfigured even if a caller
    instantiates ``RateLimitMiddleware`` without passing ``limits=``.
    """
    assert DEFAULT_LIMITS["/state/infer"] == 60
    assert DEFAULT_LIMITS["/llm/plan"] == 30
    assert DEFAULT_LIMITS["/shutdown"] == 5
    # ``/apply_intervention`` and the FastAPI-canonical ``/intervention/apply``
    # are both gated so we don't depend on a documentation alias.
    assert DEFAULT_LIMITS["/apply_intervention"] == 30
    assert DEFAULT_LIMITS["/intervention/apply"] == 30
