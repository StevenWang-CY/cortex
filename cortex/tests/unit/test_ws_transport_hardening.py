"""WS/HTTP transport hardening — Origin gate, D11 broadcast, D16 ids, stop ordering.

* ``_origin_gate``/``_is_allowed_origin`` reject the upgrade (403) for any
  web origin and accept extension/webview origins and Origin-less native
  clients — checked both against the hook and end-to-end over loopback.
* D11: a slow send is no longer cancelled after a 0.1 s total budget and
  billed as a drop; it runs to completion (bounded by the per-client
  timeout) and the broadcast is merely logged as slow.
* D16: client-supplied correlation ids are bounded/validated on both the
  HTTP header and the WS field.
* Stop ordering end-to-end with a real ``websockets`` client on an
  ephemeral loopback port: SHUTDOWN (token-gated) → SESSION_RECAP →
  SESSION_RECAP_ACKNOWLEDGED → server stop → port unbound → DB close.
  The daemon's orchestration is simulated by the shutdown callback; the
  WS server pieces are the real ones.
"""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cortex.libs.auth.local_token import load_or_create_token
from cortex.libs.config.settings import APIConfig
from cortex.services.api_gateway.request_ids import (
    MAX_CORRELATION_ID_LENGTH,
    sanitize_correlation_id,
)
from cortex.services.api_gateway.websocket_server import (
    WebSocketClient,
    WebSocketServer,
    WSMessage,
)

# ---------------------------------------------------------------------------
# Origin gate
# ---------------------------------------------------------------------------


def _status_of(response: Any) -> int:
    status = getattr(response, "status_code", None)
    if status is None:
        status = response[0]
    return int(status)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:8080",
        "http://127.0.0.1:9473",
        "https://evil.example",
        "null",
        "file://",
        "chrome-extension-not-really://abc",
        "",
    ],
)
async def test_origin_gate_rejects_web_origins(origin: str) -> None:
    server = WebSocketServer()
    assert server._is_allowed_origin(origin) is False  # noqa: SLF001
    response = await server._origin_gate(object(), SimpleNamespace(headers={"Origin": origin}))  # noqa: SLF001
    assert response is not None
    assert _status_of(response) == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "chrome-extension://abcdefghijklmnopabcdefghijklmnop"},
        {"Origin": "moz-extension://12345678-abcd"},
        {"Origin": "vscode-webview://1-abc"},
    ],
)
async def test_origin_gate_accepts_extensions_and_native_clients(headers: dict[str, str]) -> None:
    server = WebSocketServer()
    assert server._is_allowed_origin(headers.get("Origin")) is True  # noqa: SLF001
    assert await server._origin_gate(object(), SimpleNamespace(headers=headers)) is None  # noqa: SLF001


# ---------------------------------------------------------------------------
# D11 — no total-budget cancellation
# ---------------------------------------------------------------------------


class _DelayedSocket:
    def __init__(self, delay_s: float) -> None:
        self._delay_s = delay_s
        self.sent: list[str] = []
        self.closed_with: tuple[int, str] | None = None

    async def send(self, payload: str) -> None:
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        self.sent.append(payload)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed_with = (code, reason)


@pytest.mark.asyncio
async def test_slow_send_runs_to_completion_and_is_only_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = WebSocketServer()
    server._BROADCAST_BUDGET_S = 0.01  # noqa: SLF001 — slow-log threshold, not a cancel budget
    slow = _DelayedSocket(0.08)
    fast = _DelayedSocket(0.0)
    server._clients["slow"] = WebSocketClient(  # noqa: SLF001
        client_id="slow", websocket=slow, client_type="chrome", authenticated=True
    )
    server._clients["fast"] = WebSocketClient(  # noqa: SLF001
        client_id="fast", websocket=fast, client_type="vscode", authenticated=True
    )

    captured: list[dict[str, Any]] = []

    class _Logger:
        def warning(self, event: str, **fields: Any) -> None:
            captured.append({"event": event, **fields})

        def info(self, *_a: Any, **_k: Any) -> None:
            return None

        def debug(self, *_a: Any, **_k: Any) -> None:
            return None

    from cortex.libs.logging import structured as structured_mod

    monkeypatch.setattr(structured_mod, "get_logger", lambda *_a, **_k: _Logger())

    sent = await server._broadcast(  # noqa: SLF001
        WSMessage(type="INTERVENTION_TRIGGER", payload={"intervention_id": "iv1"})
    )

    assert sent == 2, "the slow client's frame was delivered, not billed as dropped"
    assert len(slow.sent) == 1 and len(fast.sent) == 1
    assert slow.closed_with is None and fast.closed_with is None
    assert set(server._clients) == {"slow", "fast"}  # noqa: SLF001
    slow_logs = [entry for entry in captured if entry["event"] == "ws_broadcast_slow"]
    assert len(slow_logs) == 1
    assert slow_logs[0]["elapsed_ms"] >= 50
    assert "dropped_for_budget" not in slow_logs[0]


# ---------------------------------------------------------------------------
# D16 — correlation-id bounds
# ---------------------------------------------------------------------------


def test_sanitize_correlation_id() -> None:
    assert sanitize_correlation_id("cid_3f9a1b2c8d0e") == "cid_3f9a1b2c8d0e"
    assert sanitize_correlation_id("ctx_chrome_12") == "ctx_chrome_12"
    assert sanitize_correlation_id("a" * MAX_CORRELATION_ID_LENGTH) is not None
    assert sanitize_correlation_id("a" * (MAX_CORRELATION_ID_LENGTH + 1)) is None
    assert sanitize_correlation_id("bad id") is None
    assert sanitize_correlation_id("evil\nline") is None
    assert sanitize_correlation_id("") is None
    assert sanitize_correlation_id(None) is None
    assert sanitize_correlation_id(12) is None


def test_http_middleware_replaces_junk_request_ids() -> None:
    from cortex.services.api_gateway.app import create_app, registry

    registry.reset()
    try:
        client = TestClient(create_app())
        junk = "x" * 5000
        echoed = client.get("/health", headers={"X-Cortex-Request-ID": junk}).headers[
            "X-Cortex-Request-ID"
        ]
        assert echoed != junk and echoed.startswith("cid_")
        control = client.get(
            "/health", headers={"X-Cortex-Request-ID": "bad id\twith-control"}
        ).headers["X-Cortex-Request-ID"]
        assert control.startswith("cid_")
        assert (
            client.get("/health", headers={"X-Cortex-Request-ID": "cid-ok_1.2:3"}).headers[
                "X-Cortex-Request-ID"
            ]
            == "cid-ok_1.2:3"
        )
    finally:
        registry.reset()


@pytest.mark.asyncio
async def test_ws_junk_correlation_id_is_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    server = WebSocketServer()
    seen: list[str | None] = []

    async def spy_dispatch(_client: WebSocketClient, msg: WSMessage) -> None:
        seen.append(msg.correlation_id)

    monkeypatch.setattr(server, "_dispatch_message", spy_dispatch)

    class _Sock:
        async def send(self, _raw: str) -> None:
            return None

    client = WebSocketClient(client_id="c", websocket=_Sock())
    junk = "j" * 5000
    await server._process_message(  # noqa: SLF001
        client,
        json.dumps({"type": "AUTH", "payload": {}, "timestamp": 0, "sequence": 0,
                    "correlation_id": junk}),
    )
    await server._process_message(  # noqa: SLF001
        client,
        json.dumps({"type": "AUTH", "payload": {}, "timestamp": 0, "sequence": 0,
                    "correlation_id": "cid_keep_me"}),
    )
    assert len(seen) == 2
    assert seen[0] is not None and seen[0] != junk
    assert sanitize_correlation_id(seen[0]) == seen[0]
    assert seen[1] == "cid_keep_me"


# ---------------------------------------------------------------------------
# End-to-end stop ordering over loopback
# ---------------------------------------------------------------------------


def _bound_port(server: WebSocketServer) -> int:
    sockets = server._server.sockets  # noqa: SLF001
    return int(sockets[0].getsockname()[1])


@pytest.mark.asyncio
async def test_shutdown_orders_recap_ack_close_and_unbinds_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    websockets = pytest.importorskip("websockets")
    from websockets.exceptions import ConnectionClosed, InvalidStatus

    token_file = tmp_path / "auth.token"
    monkeypatch.setattr("cortex.libs.auth.local_token.auth_token_path", lambda: token_file)
    token = load_or_create_token(token_file)

    server = WebSocketServer(config=APIConfig(host="127.0.0.1", ws_port=0))
    events: list[str] = []
    ack_received = asyncio.Event()
    finished = asyncio.Event()

    class _FakeDatabase:
        closed = False

        async def close(self) -> None:
            self.closed = True
            events.append("db-closed")

    database = _FakeDatabase()

    async def orchestrate_stop() -> None:
        # Mirrors CortexDaemon._stop_once: recap → bounded ack wait →
        # WS teardown (ports unbound) → SQLite close.
        await server.send_message("SESSION_RECAP", {"session_id": "s1", "persisted": True})
        events.append("recap-sent")
        try:
            await asyncio.wait_for(ack_received.wait(), timeout=5.0)
        finally:
            events.append("ws-stopping")
            await server.stop()
            events.append("ws-stopped")
            await database.close()
            finished.set()

    async def on_shutdown() -> None:
        events.append("shutdown-requested")
        asyncio.get_running_loop().create_task(orchestrate_stop())

    async def on_recap_ack(session_id: str | None) -> None:
        events.append(f"ack:{session_id}")
        ack_received.set()

    server.set_shutdown_callback(on_shutdown)
    server.set_session_recap_acknowledged_callback(on_recap_ack)
    assert await server.start() is True
    port = _bound_port(server)
    uri = f"ws://127.0.0.1:{port}"

    # A web origin is refused at the upgrade, before AUTH is even possible.
    with pytest.raises(InvalidStatus) as rejected:
        async with websockets.connect(uri, origin="http://localhost:8080"):
            pass
    assert rejected.value.response.status_code == 403

    client_frames: list[dict[str, Any]] = []
    closed_cleanly = False
    async with websockets.connect(uri, open_timeout=5) as ws:
        await ws.send(json.dumps({"type": "AUTH", "payload": {"auth_token": token},
                                  "timestamp": 0, "sequence": 0}))
        auth_ok = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        assert auth_ok["type"] == "AUTH_OK"
        await ws.send(json.dumps({"type": "SHUTDOWN", "payload": {"auth_token": token},
                                  "timestamp": 0, "sequence": 1}))
        try:
            while True:
                frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                client_frames.append(frame)
                if frame["type"] == "SESSION_RECAP":
                    await ws.send(json.dumps({
                        "type": "SESSION_RECAP_ACKNOWLEDGED",
                        "payload": {"session_id": frame["payload"]["session_id"]},
                        "timestamp": 0, "sequence": 2,
                    }))
        except ConnectionClosed:
            closed_cleanly = True

    await asyncio.wait_for(finished.wait(), timeout=10)

    assert closed_cleanly, "the server must close the socket after the ack"
    assert [f["type"] for f in client_frames].count("SESSION_RECAP") == 1
    assert events == [
        "shutdown-requested",
        "recap-sent",
        "ack:s1",
        "ws-stopping",
        "ws-stopped",
        "db-closed",
    ]
    assert database.closed is True
    assert server.is_running is False
    with pytest.raises((ConnectionRefusedError, OSError)):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()


@pytest.mark.asyncio
async def test_shutdown_without_token_is_ignored_over_loopback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    websockets = pytest.importorskip("websockets")

    token_file = tmp_path / "auth.token"
    monkeypatch.setattr("cortex.libs.auth.local_token.auth_token_path", lambda: token_file)
    token = load_or_create_token(token_file)

    server = WebSocketServer(config=APIConfig(host="127.0.0.1", ws_port=0))
    fired = asyncio.Event()

    async def on_shutdown() -> None:
        fired.set()

    server.set_shutdown_callback(on_shutdown)
    assert await server.start() is True
    port = _bound_port(server)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}", open_timeout=5) as ws:
            await ws.send(json.dumps({"type": "AUTH", "payload": {"auth_token": token},
                                      "timestamp": 0, "sequence": 0}))
            assert json.loads(await asyncio.wait_for(ws.recv(), timeout=5))["type"] == "AUTH_OK"
            await ws.send(json.dumps({"type": "SHUTDOWN", "payload": {},
                                      "timestamp": 0, "sequence": 1}))
            await asyncio.sleep(0.2)
        assert not fired.is_set(), "SHUTDOWN without the token must not fire the callback"
    finally:
        await server.stop()
    with pytest.raises((ConnectionRefusedError, OSError)):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
