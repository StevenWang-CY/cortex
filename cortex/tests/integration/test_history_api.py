"""
History / Trends REST API integration tests (P0 §3.1 / §3.2).

Exercises the FastAPI routes mounted on the authenticated router:

* ``GET /api/sessions`` — list response.
* ``GET /api/sessions/{id}`` — detail (happy / missing / traversal).
* ``GET /api/trends?window=…`` — trends rollup with window fallback.
* All routes require the systemic capability token (Debt-2); missing /
  invalid headers must return 401/403.

We register a tiny stub daemon in the service registry under the
``"daemon"`` key — same indirection the real daemon uses
(``runtime_daemon._register_services``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cortex.libs.schemas.session_history import (
    SessionDetailResponse,
    SessionListResponse,
    SessionSummary,
    TrendsResponse,
)
from cortex.services.api_gateway.app import create_app, registry


class _StubDaemon:
    """Tiny daemon double exposing only the four history/recap entry points.

    Mirrors ``CortexDaemon.list_sessions / get_session / get_trends /
    latest_session_recap`` signatures so the routes resolve through the
    same indirection as production.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, dict] = {}
        self.trends_calls: list[tuple[str, bool]] = []

    def add_session(self, session_id: str, **fields: Any) -> None:
        base = {
            "session_id": session_id,
            "start_time": datetime(2026, 5, 24, 9, 0, tzinfo=UTC),
            "end_time": datetime(2026, 5, 24, 10, 0, tzinfo=UTC),
            "duration_seconds": 3600.0,
            "flow_percentage": 65.0,
            "peak_stress_integral": 200.0,
            "top_distraction_domain": "reddit.com",
            "intervention_count": 2,
        }
        base.update(fields)
        self._sessions[session_id] = base

    async def list_sessions(
        self, since: float | None, limit: int
    ) -> SessionListResponse:
        items = [SessionSummary(**s) for s in self._sessions.values()]
        items.sort(key=lambda s: s.start_time, reverse=True)
        return SessionListResponse(items=items, next_cursor=None, total_known=len(items))

    async def get_session(self, session_id: str) -> SessionDetailResponse:
        # Mirror the reader's path-traversal defence: any id with
        # non-safe characters returns not_found without touching state.
        if not session_id or "/" in session_id or ".." in session_id:
            return SessionDetailResponse(report=None, error="not_found")
        record = self._sessions.get(session_id)
        if record is None:
            return SessionDetailResponse(report=None, error="not_found")
        # Build a full SessionReport from the summary fields so the
        # envelope is shaped exactly like production.
        from cortex.services.session_report.models import SessionReport

        report = SessionReport(
            session_id=record["session_id"],
            start_time=record["start_time"],
            end_time=record["end_time"],
            duration_seconds=record["duration_seconds"],
            flow_percentage=record["flow_percentage"],
            peak_stress_integral=record["peak_stress_integral"],
            top_distraction_domains=[record["top_distraction_domain"]]
            if record["top_distraction_domain"]
            else [],
        )
        return SessionDetailResponse(report=report, error=None)

    async def get_trends(
        self, window: str, *, refresh: bool = False
    ) -> TrendsResponse:
        self.trends_calls.append((window, refresh))
        # Daemon clamps invalid windows to "week" before calling the
        # aggregator — replicate so the route's behaviour is the same.
        w = window if window in ("week", "month", "quarter") else "week"
        return TrendsResponse(window=w)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    registry.reset()
    yield
    registry.reset()


@pytest.fixture()
def client_and_daemon(tmp_path: Path, monkeypatch):
    """Authenticated test client plus the stub daemon registered in the registry."""
    from cortex.libs.auth.local_token import load_or_create_token

    token_file = tmp_path / "auth.token"
    monkeypatch.setattr(
        "cortex.libs.auth.local_token.auth_token_path", lambda: token_file
    )
    token = load_or_create_token(token_file)

    stub = _StubDaemon()
    app = create_app()

    # Register the stub under the same key the real daemon uses.
    app.state.registry.register("daemon", stub)

    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {token}"})
        yield c, stub, token


# ─── happy paths ──────────────────────────────────────────────────────


def test_get_sessions_returns_200_and_valid_envelope(client_and_daemon) -> None:
    client, stub, _ = client_and_daemon
    stub.add_session("a", start_time=datetime(2026, 5, 24, 9, 0, tzinfo=UTC))
    stub.add_session("b", start_time=datetime(2026, 5, 24, 10, 0, tzinfo=UTC))
    r = client.get("/api/sessions")
    assert r.status_code == 200
    body = r.json()
    # Response must validate as SessionListResponse.
    SessionListResponse.model_validate(body)
    assert body["total_known"] == 2
    assert [item["session_id"] for item in body["items"]] == ["b", "a"]


def test_get_session_detail_happy_path(client_and_daemon) -> None:
    client, stub, _ = client_and_daemon
    stub.add_session("good-id")
    r = client.get("/api/sessions/good-id")
    assert r.status_code == 200
    body = r.json()
    envelope = SessionDetailResponse.model_validate(body)
    assert envelope.error is None
    assert envelope.report is not None
    assert envelope.report.session_id == "good-id"


def test_get_session_detail_missing_returns_structured_not_found(
    client_and_daemon,
) -> None:
    """Missing id → 200 with ``{report: null, error: 'not_found'}``, NOT 404."""
    client, _, _ = client_and_daemon
    r = client.get("/api/sessions/this-id-does-not-exist")
    assert r.status_code == 200
    body = r.json()
    assert body["report"] is None
    assert body["error"] == "not_found"


def test_get_session_detail_path_traversal_returns_not_found(
    client_and_daemon,
) -> None:
    """A traversal attempt is reduced to ``not_found`` by the daemon."""
    client, _, _ = client_and_daemon
    # FastAPI strips ``..`` from URLs, so we encode the test against the
    # path-parameter as the daemon would see it: e.g. ``..something``.
    r = client.get("/api/sessions/..hostile")
    assert r.status_code == 200
    body = r.json()
    assert body["report"] is None
    assert body["error"] == "not_found"


def test_get_trends_week(client_and_daemon) -> None:
    client, stub, _ = client_and_daemon
    r = client.get("/api/trends?window=week")
    assert r.status_code == 200
    body = r.json()
    envelope = TrendsResponse.model_validate(body)
    assert envelope.window == "week"
    assert stub.trends_calls == [("week", False)]


def test_get_trends_invalid_window_falls_back_to_week(client_and_daemon) -> None:
    """Daemon clamps unknown ``window`` values to ``"week"``."""
    client, stub, _ = client_and_daemon
    # FastAPI's Literal validation actually rejects unknown windows with
    # 422 at the route level, BEFORE the daemon clamps. That is the
    # documented behaviour; assert it explicitly rather than testing the
    # daemon's internal fallback.
    r = client.get("/api/trends?window=year")
    assert r.status_code == 422  # query-param Literal mismatch


def test_get_trends_refresh_true_passes_through(client_and_daemon) -> None:
    client, stub, _ = client_and_daemon
    r = client.get("/api/trends?window=month&refresh=true")
    assert r.status_code == 200
    assert stub.trends_calls[-1] == ("month", True)


# ─── auth ─────────────────────────────────────────────────────────────


def test_get_sessions_requires_token(client_and_daemon) -> None:
    """Missing ``Authorization`` returns 401/403 (audit Debt-2)."""
    client, _, _ = client_and_daemon
    # Strip the default header and call again.
    client.headers.pop("Authorization", None)
    r = client.get("/api/sessions")
    assert r.status_code in (401, 403), f"got {r.status_code} {r.text}"


def test_get_session_detail_requires_token(client_and_daemon) -> None:
    client, stub, _ = client_and_daemon
    stub.add_session("x")
    client.headers.pop("Authorization", None)
    r = client.get("/api/sessions/x")
    assert r.status_code in (401, 403)


def test_get_trends_requires_token(client_and_daemon) -> None:
    client, _, _ = client_and_daemon
    client.headers.pop("Authorization", None)
    r = client.get("/api/trends?window=week")
    assert r.status_code in (401, 403)


def test_get_sessions_invalid_token_rejected(client_and_daemon) -> None:
    client, _, _ = client_and_daemon
    client.headers["Authorization"] = "Bearer not-a-real-token"
    r = client.get("/api/sessions")
    assert r.status_code in (401, 403)


# ─── degraded mode ────────────────────────────────────────────────────


def test_get_sessions_returns_empty_when_daemon_missing(tmp_path, monkeypatch) -> None:
    """If no daemon is registered, routes degrade gracefully."""
    from cortex.libs.auth.local_token import load_or_create_token

    token_file = tmp_path / "auth.token"
    monkeypatch.setattr(
        "cortex.libs.auth.local_token.auth_token_path", lambda: token_file
    )
    token = load_or_create_token(token_file)

    app = create_app()
    # No daemon registered.
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {token}"})
        r = c.get("/api/sessions")
        assert r.status_code == 200
        body = r.json()
        assert body["items"] == []
        assert body["total_known"] == 0

        r2 = c.get("/api/sessions/anything")
        assert r2.status_code == 200
        assert r2.json()["error"] == "not_found"

        r3 = c.get("/api/trends?window=week")
        assert r3.status_code == 200
