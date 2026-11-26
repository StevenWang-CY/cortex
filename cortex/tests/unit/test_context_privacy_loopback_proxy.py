"""The privacy controller's loopback requests must never go through a proxy.

``urllib.request.urlopen`` installs a ``ProxyHandler`` built from
``getproxies()``: the ``HTTP_PROXY`` environment, or on macOS the System
Settings proxy, whose default bypass list (``*.local, 169.254/16``) does not
include ``127.0.0.1``. With a proxy configured -- common on managed Macs and
for anyone running a debugging proxy -- every Settings → "Review request…"
call, carrying ``Authorization: Bearer <capability token>`` and the redacted
workspace preview, was sent to the proxy instead of the local daemon. The
user saw "daemon is not reachable" and the token left the machine.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from cortex.apps.desktop_shell import context_privacy_controller as cpc


class _Recorder(BaseHTTPRequestHandler):
    hits: list[tuple[str, str | None]]

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self.server.hits.append((self.path, self.headers.get("Authorization")))  # type: ignore[attr-defined]
        body = b'{"network_allowed_by_configuration": false}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        return


def _serve() -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    server.hits = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


@pytest.fixture()
def servers() -> Iterator[tuple[ThreadingHTTPServer, ThreadingHTTPServer]]:
    target, target_thread = _serve()
    proxy, proxy_thread = _serve()
    try:
        yield target, proxy
    finally:
        for server, thread in ((target, target_thread), (proxy, proxy_thread)):
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def test_status_request_bypasses_a_configured_proxy(
    servers: tuple[ThreadingHTTPServer, ThreadingHTTPServer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target, proxy = servers
    proxy_url = f"http://127.0.0.1:{proxy.server_address[1]}"
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTP_PROXY", proxy_url)
    monkeypatch.setenv("http_proxy", proxy_url)
    monkeypatch.setattr(cpc, "load_or_create_token", lambda: "loopback-only-token")

    controller = cpc.ContextPrivacyController()
    controller._base_url = f"http://127.0.0.1:{target.server_address[1]}"
    received: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    controller.status_received.connect(received.append)
    controller.request_failed.connect(failures.append)

    # Run the worker body inline: the emits are direct, same-thread calls.
    controller._request_worker("status", "GET", "/privacy/context/status", None, 3.0)

    assert proxy.hits == [], "the capability token was sent to the proxy"  # type: ignore[attr-defined]
    assert target.hits == [  # type: ignore[attr-defined]
        ("/privacy/context/status", "Bearer loopback-only-token")
    ]
    assert failures == []
    assert received == [{"network_allowed_by_configuration": False}]
