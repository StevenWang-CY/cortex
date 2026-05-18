"""Audit F08 — launcher /stop capability-token gate.

Boots the launcher's ``LauncherHandler`` against an ephemeral port and
verifies that POST /stop is 401 without the token, 200 with the
matching token, and that the unrelated /health and /status endpoints
remain reachable without authentication (they are non-destructive).
"""

from __future__ import annotations

import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from cortex.scripts import launcher_agent


@pytest.fixture
def token_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the launcher's inline token-path resolver at a temp file."""
    path = tmp_path / "auth.token"
    path.write_text("a" * 64 + "\n", encoding="utf-8")
    monkeypatch.setattr(launcher_agent, "_auth_token_path", lambda: str(path))
    return path


@pytest.fixture
def running_launcher(monkeypatch: pytest.MonkeyPatch):
    """Run LauncherHandler on an ephemeral port; clean up on teardown.

    ``_stop_daemon`` is patched to a no-op so the test does not actually
    kill any local daemon process if one is running on the developer
    machine.
    """
    monkeypatch.setattr(
        launcher_agent, "_stop_daemon", lambda: {"status": "stopped"}
    )
    monkeypatch.setattr(
        launcher_agent, "_launch_daemon", lambda: {"status": "launched"}
    )

    server = ThreadingHTTPServer(("127.0.0.1", 0), launcher_agent.LauncherHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _post(port: int, path: str, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        method="POST",
        data=b"",
        headers=headers or {},
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_stop_without_token_is_rejected(token_file: Path, running_launcher: int) -> None:
    status, body = _post(running_launcher, "/stop")
    assert status == 401, body


def test_stop_with_wrong_token_is_rejected(token_file: Path, running_launcher: int) -> None:
    status, body = _post(
        running_launcher,
        "/stop",
        headers={"X-Cortex-Auth-Token": "definitely-not-the-real-token"},
    )
    assert status == 401, body


def test_stop_with_correct_token_succeeds(token_file: Path, running_launcher: int) -> None:
    presented = token_file.read_text(encoding="utf-8").strip()
    status, body = _post(
        running_launcher,
        "/stop",
        headers={"X-Cortex-Auth-Token": presented},
    )
    assert status == 200, body


def test_health_remains_unauthenticated(token_file: Path, running_launcher: int) -> None:
    """The /health endpoint is intentionally open — it is the liveness
    probe used by `_check_existing_launcher` to detect "already running"
    on startup. Gating it would break supervisor restart logic."""
    with urllib.request.urlopen(
        f"http://127.0.0.1:{running_launcher}/health", timeout=2
    ) as resp:
        assert resp.status == 200


def test_stop_with_missing_token_file_falls_closed(
    tmp_path: Path,
    running_launcher: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the token file is absent (fresh install, file deleted), the
    gate must reject — not fall open."""
    monkeypatch.setattr(
        launcher_agent, "_auth_token_path", lambda: str(tmp_path / "nonexistent")
    )
    status, _ = _post(
        running_launcher,
        "/stop",
        headers={"X-Cortex-Auth-Token": "any-value-at-all"},
    )
    assert status == 401
