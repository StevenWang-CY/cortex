"""D4 — ``GET /health`` is unauthenticated and unlimited, so it must be DB-free.

The route used to await ``storage_maintenance.health()`` — a ``PRAGMA
quick_check`` (O(database)) plus record counts on the single SQLite worker
— on every call from any localhost origin. It now reports in-memory
readiness fields and the *cached* snapshot left behind by the last
authenticated ``/storage/status`` probe.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from cortex.libs.auth.local_token import load_or_create_token
from cortex.libs.schemas.storage import StorageHealthReport
from cortex.services.api_gateway.app import create_app, registry


class _ExplodingDatabase:
    """Any database call from ``/health`` is a regression."""

    async def read(self, *_a: object, **_k: object) -> None:
        raise AssertionError("/health touched the database (read)")

    async def transaction(self, *_a: object, **_k: object) -> None:
        raise AssertionError("/health touched the database (transaction)")

    async def health(self, *_a: object, **_k: object) -> None:
        raise AssertionError("/health touched the database (health)")


class _CountingMaintenance:
    retention_days = {"sessions": 180, "policy": 90, "interventions": 90}

    def __init__(self) -> None:
        self.health_calls = 0
        self._last: StorageHealthReport | None = None

    @property
    def last_health_report(self) -> StorageHealthReport | None:
        return self._last

    async def health(self) -> StorageHealthReport:
        self.health_calls += 1
        self._last = StorageHealthReport(
            healthy=True,
            degraded=False,
            journal_mode="delete",
            synchronous="full",
            foreign_keys=True,
            schema_version=2,
            sqlite_version="3.45.0",
            database_filename="cortex.sqlite3",
            database_bytes=4096,
            pending_operations=0,
            record_counts={"sessions": 1},
        )
        return self._last


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    token_file = tmp_path / "auth.token"
    monkeypatch.setattr("cortex.libs.auth.local_token.auth_token_path", lambda: token_file)
    token = load_or_create_token(token_file)
    registry.reset()
    app = create_app()
    with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as c:
        yield c
    registry.reset()


def test_health_never_probes_storage_but_reports_cached_snapshot(client: TestClient) -> None:
    maintenance = _CountingMaintenance()
    registry.register("storage_maintenance", maintenance)
    registry.register("database", _ExplodingDatabase())

    for _ in range(25):
        resp = client.get("/health", headers={"Authorization": ""})
        assert resp.status_code == 200, resp.text
    assert maintenance.health_calls == 0
    assert resp.json()["storage"] is None

    # The authenticated probe runs the live check and primes the cache …
    status = client.get("/storage/status")
    assert status.status_code == 200, status.text
    assert maintenance.health_calls == 1

    # … which /health then reports without probing again.
    cached = client.get("/health", headers={"Authorization": ""}).json()
    assert cached["storage"]["journal_mode"] == "delete"
    assert cached["store_degraded"] is False
    assert maintenance.health_calls == 1


def test_health_reports_readiness_fields(client: TestClient) -> None:
    pipeline = SimpleNamespace(
        is_running=True,
        capture_stale=False,
        frames_dropped_total=0,
        camera_recovery_attempts=0,
        camera_recovery_successes=0,
    )
    daemon = SimpleNamespace(
        is_ready=True,
        _capture_pipeline=pipeline,
        _duplicate_intervention_ack_count=0,
        _store_degraded=False,
    )
    registry.register("daemon", daemon)
    registry.register("ws_server", SimpleNamespace(is_running=True))

    data = client.get("/health").json()
    assert data["ready"] is True
    assert data["ws_listening"] is True
    assert data["capture_state"] == "running"

    pipeline.capture_stale = True
    assert client.get("/health").json()["capture_state"] == "stale"

    pipeline.capture_stale = False
    pipeline.is_running = False
    daemon.is_ready = False
    registry.register("ws_server", SimpleNamespace(is_running=False))
    data = client.get("/health").json()
    assert data["capture_state"] == "stopped"
    assert data["ready"] is False
    assert data["ws_listening"] is False


def test_health_without_daemon_reports_unavailable_capture(client: TestClient) -> None:
    data = client.get("/health").json()
    assert data["capture_state"] == "unavailable"
    assert data["ws_listening"] is False
    assert data["storage"] is None


def test_health_reflects_store_healthy_flag(client: TestClient) -> None:
    registry.register("store_healthy", False)
    assert client.get("/health").json()["store_degraded"] is True
