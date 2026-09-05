"""D3 — rate-limit budget is consumed only after the capability token validates.

``RateLimitMiddleware`` wraps the whole app while auth is a route
dependency, and every local client shares the ``127.0.0.1`` bucket. An
unauthenticated localhost page could therefore exhaust ``/shutdown``,
``/consent/reset`` and ``/api/launch`` and starve the real clients. With
``authenticated_only`` (the production default) unauthenticated requests
never touch a bucket; the route's dependency answers 401.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cortex.libs.auth.local_token import load_or_create_token
from cortex.services.api_gateway.middleware.rate_limit import RateLimitMiddleware


@pytest.fixture()
def auth_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    token_file = tmp_path / "auth.token"
    monkeypatch.setattr("cortex.libs.auth.local_token.auth_token_path", lambda: token_file)
    return load_or_create_token(token_file)


def _build_app(limits: dict[str, int]) -> tuple[FastAPI, RateLimitMiddleware]:
    """Limiter with the production default (``authenticated_only=True``)
    on routes that have NO auth dependency, so an unauthenticated request
    reaching the handler proves the limiter let it through unbilled."""
    app = FastAPI()
    captured: dict[str, RateLimitMiddleware] = {}

    class _Capturing(RateLimitMiddleware):
        def __init__(self, app, **kw):  # type: ignore[no-untyped-def]
            super().__init__(app, **kw)
            captured["m"] = self

    app.add_middleware(_Capturing, limits=limits)

    @app.post("/shutdown")
    async def _shutdown() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app):
        pass
    return app, captured["m"]


def _feature_vector_payload() -> dict[str, object]:
    return {
        "feature_vector": {
            "timestamp": 1.0,
            "hr": 72.0,
            "hrv_rmssd": 50.0,
            "hr_delta": 1.0,
            "blink_rate": 16.0,
            "blink_rate_delta": -1.0,
            "shoulder_drop_ratio": 0.05,
            "forward_lean_angle": 5.0,
            "mouse_velocity_mean": 500.0,
            "mouse_velocity_variance": 5000.0,
            "click_frequency": 0.5,
            "keystroke_interval_variance": 500.0,
            "tab_switch_frequency": 5.0,
        },
        "signal_quality": {"physio": 0.8, "kinematics": 0.7, "telemetry": 0.9},
    }


def test_default_is_authenticated_only() -> None:
    _app, limiter = _build_app({"/shutdown": 1})
    assert limiter.authenticated_only is True


def test_unauthenticated_requests_never_consume_budget(auth_token: str) -> None:
    app, limiter = _build_app({"/shutdown": 1})
    with TestClient(app) as client:
        for _ in range(10):
            assert client.post("/shutdown").status_code == 200
        assert limiter._buckets == {}, "unauthenticated calls must not bill a bucket"

        wrong = {"X-Cortex-Auth-Token": "0" * 64}
        for _ in range(10):
            assert client.post("/shutdown", headers=wrong).status_code == 200
        assert limiter._buckets == {}

        good = {"X-Cortex-Auth-Token": auth_token}
        assert client.post("/shutdown", headers=good).status_code == 200
        assert client.post("/shutdown", headers=good).status_code == 429


def test_bearer_header_is_billed_the_same_way(auth_token: str) -> None:
    app, limiter = _build_app({"/shutdown": 1})
    with TestClient(app) as client:
        assert (
            client.post("/shutdown", headers={"Authorization": f"Bearer {auth_token}"}).status_code
            == 200
        )
        assert len(limiter._buckets) == 1
        assert (
            client.post("/shutdown", headers={"Authorization": f"Bearer {auth_token}"}).status_code
            == 429
        )


def test_full_app_unauthenticated_gets_401_and_budget_survives(auth_token: str) -> None:
    """Through ``create_app``: a flood of tokenless calls yields 401s (never
    429) and leaves the authenticated caller's full budget intact."""
    from cortex.services.api_gateway.app import create_app, registry

    registry.reset()
    app = create_app()
    for mw in app.user_middleware:
        cls = mw.cls if hasattr(mw, "cls") else mw[0]
        if cls is RateLimitMiddleware:
            kwargs = mw.kwargs if hasattr(mw, "kwargs") else mw[1]
            assert kwargs.get("authenticated_only") is True
            kwargs["limits"] = {"/state/infer": 1}
            break
    else:  # pragma: no cover - wiring regression
        raise AssertionError("RateLimitMiddleware is not wired")

    try:
        with TestClient(app) as client:
            for _ in range(10):
                resp = client.post("/state/infer", json=_feature_vector_payload())
                assert resp.status_code == 401, resp.text
            auth = {"Authorization": f"Bearer {auth_token}"}
            first = client.post("/state/infer", json=_feature_vector_payload(), headers=auth)
            assert first.status_code == 200, first.text
            second = client.post("/state/infer", json=_feature_vector_payload(), headers=auth)
            assert second.status_code == 429, second.text
    finally:
        registry.reset()
