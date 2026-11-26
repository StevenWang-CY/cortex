"""The helper scripts reach the daemon on loopback, whatever proxy is configured.

``urllib.request.urlopen`` builds its opener from ``getproxies()``. With an
HTTP proxy configured (environment or the macOS system setting) the native
host, the launcher agent and the launcher installer sent their 127.0.0.1
requests to the proxy: the health check read as "not running" and
``/shutdown`` never arrived, so Stop fell through to signals.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from cortex.scripts import install_launcher, launcher_agent, native_host


class _Handler(BaseHTTPRequestHandler):
    def _answer(self) -> None:
        body = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self._answer()

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self._answer()

    def log_message(self, *_args: object) -> None:
        return None


@pytest.fixture()
def loopback_port() -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture()
def unreachable_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    # Port 1 refuses connections: anything routed to this proxy fails at once.
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)


def test_the_configured_proxy_really_breaks_a_plain_urlopen(
    loopback_port: int, unreachable_proxy: None
) -> None:
    """Control: without the bypass this environment cannot reach loopback."""

    # A fresh default opener reads the proxy configuration exactly as
    # ``urlopen`` would. ``urlopen`` itself is not called here: it would cache
    # a proxied global opener for every later test in the process.
    default_opener = urllib.request.build_opener()
    with pytest.raises((urllib.error.URLError, OSError)):
        default_opener.open(f"http://127.0.0.1:{loopback_port}/health", timeout=2)


def test_launcher_agent_reaches_the_daemon_through_a_configured_proxy(
    loopback_port: int, unreachable_proxy: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launcher_agent, "DAEMON_HTTP_PORT", loopback_port)
    assert launcher_agent._daemon_http_alive() is True
    with launcher_agent._LOOPBACK_OPENER.open(
        f"http://127.0.0.1:{loopback_port}/health", timeout=2
    ) as response:
        assert response.status == 200


def test_native_host_raises_the_dashboard_through_a_configured_proxy(
    loopback_port: int, unreachable_proxy: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(native_host, "HTTP_API_PORT", loopback_port)
    monkeypatch.setattr(native_host, "_read_auth_token", lambda: "token")
    assert native_host._raise_dashboard("dashboard") == {"ok": True, "status": 200}


def test_launcher_installer_health_check_ignores_a_configured_proxy(
    loopback_port: int, unreachable_proxy: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(install_launcher, "LAUNCHER_PORT", loopback_port)
    assert install_launcher._check_launcher_health() is True
