"""Audit-prod G1 / Audit-2 — launcher /launch CSRF gate.

Mirrors the existing /stop gate (``test_launcher_auth.py``). Prior to
the audit-2 sweep, ``/launch`` was wide-open while ``/stop`` required
the token — combined with ``Access-Control-Allow-Origin: *``, any open
browser tab could force-start the daemon (which then opened the camera
and began capturing biometrics) without user consent. These tests pin
the symmetric gating.
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
    path = tmp_path / "auth.token"
    path.write_text("b" * 64 + "\n", encoding="utf-8")
    monkeypatch.setattr(launcher_agent, "_auth_token_path", lambda: str(path))
    return path


@pytest.fixture
def running_launcher(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        launcher_agent, "_launch_daemon", lambda: {"status": "launched"}
    )
    monkeypatch.setattr(
        launcher_agent, "_stop_daemon", lambda: {"status": "stopped"}
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


def _post(port: int, path: str, headers: dict[str, str] | None = None) -> int:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        method="POST",
        data=b"",
        headers=headers or {},
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_launch_without_token_is_rejected(
    token_file: Path, running_launcher: int,
) -> None:
    """Any cross-origin page POSTing /launch with no token must be denied."""
    assert _post(running_launcher, "/launch") == 401


def test_launch_with_wrong_token_is_rejected(
    token_file: Path, running_launcher: int,
) -> None:
    assert _post(
        running_launcher,
        "/launch",
        headers={"X-Cortex-Auth-Token": "not-the-token"},
    ) == 401


def test_launch_with_correct_token_succeeds(
    token_file: Path, running_launcher: int,
) -> None:
    presented = token_file.read_text(encoding="utf-8").strip()
    assert _post(
        running_launcher,
        "/launch",
        headers={"X-Cortex-Auth-Token": presented},
    ) == 200


def test_launch_with_missing_token_file_falls_closed(
    tmp_path: Path,
    running_launcher: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the auth.token file is absent, the gate must reject any
    presented value (fall-closed, not fall-open)."""
    monkeypatch.setattr(
        launcher_agent,
        "_auth_token_path",
        lambda: str(tmp_path / "nonexistent"),
    )
    assert _post(
        running_launcher,
        "/launch",
        headers={"X-Cortex-Auth-Token": "any"},
    ) == 401
