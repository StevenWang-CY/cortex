"""F22 — Slow WS client gets an explicit close on disconnect.

The server's ``_broadcast`` used to silently drop a client whose ``send``
exceeded the 1 s timeout. The extension then saw an EPIPE on the next
send instead of a clean close, which made reconnection slow and noisy.
F22 emits ``close(code=1011, reason="slow consumer")`` before removing
the client and logs an ``EventType.WS_CLIENT_DISCONNECTED`` event with
the client id and reason.

Run with: ``pytest cortex/tests/unit/test_ws_slow_client.py``
"""

from __future__ import annotations

import asyncio

import pytest

from cortex.services.api_gateway.websocket_server import (
    WebSocketClient,
    WebSocketServer,
    WSMessage,
)


class _SlowSocket:
    """A stub websocket whose ``send`` never completes — triggers the
    1 s timeout inside ``_broadcast``."""

    def __init__(self) -> None:
        self.closed_with: tuple[int, str] | None = None
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        # Sleep longer than _broadcast's 1 s timeout so asyncio.wait_for
        # raises and we land in the dead-client branch.
        await asyncio.sleep(5)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed_with = (code, reason)


class _HealthySocket:
    """A stub websocket whose ``send`` resolves immediately."""

    def __init__(self) -> None:
        self.closed_with: tuple[int, str] | None = None
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed_with = (code, reason)


class _AlreadyDeadSocket:
    """A stub whose close() raises (peer already torn down)."""

    async def send(self, payload: str) -> None:
        await asyncio.sleep(5)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        raise ConnectionError("already dead")


@pytest.mark.asyncio
async def test_slow_client_receives_close_frame_and_is_removed(monkeypatch):
    server = WebSocketServer()
    # F22 + Phase-I coordination: the F22 close-on-slow-consumer path
    # only fires when the PER-SEND timeout elapses (a truly dead
    # client). Phase I added a tighter total-broadcast BUDGET which
    # otherwise cancels the slow task first and reroutes it to the
    # "slow but alive" branch. For the F22 disconnect contract we
    # widen the budget here so per-send fires first; the tighter
    # 100 ms budget is exercised by ``test_broadcast_throughput.py``.
    server._BROADCAST_PER_CLIENT_TIMEOUT_S = 0.2
    server._BROADCAST_BUDGET_S = 5.0
    slow_ws = _SlowSocket()
    client = WebSocketClient(
        client_id="c-slow", websocket=slow_ws, client_type="chrome"
    )
    server._clients["c-slow"] = client

    # Capture structured-logger calls so we can assert the
    # ws_client_disconnected event was emitted with the right fields.
    captured: list[dict] = []

    class _CapturedLogger:
        def info(self, event: str, **fields):
            captured.append({"event": event, **fields})

        def debug(self, *_a, **_kw): pass
        def warning(self, *_a, **_kw): pass

    from cortex.libs.logging import structured as structured_mod
    monkeypatch.setattr(
        structured_mod, "get_logger", lambda *_a, **_kw: _CapturedLogger()
    )

    msg = WSMessage(type="STATE_UPDATE", payload={"state": "FLOW"})
    sent = await server._broadcast(msg)

    assert sent == 0, "slow client should not count as sent"
    assert "c-slow" not in server._clients, "slow client must be removed"
    assert slow_ws.closed_with == (1011, "slow consumer"), (
        f"expected close(1011, slow consumer); got {slow_ws.closed_with}"
    )
    # Structured disconnect event surfaced with the client id + reason.
    disconnect = [
        entry for entry in captured
        if entry.get("event_type") == "ws_client_disconnected"
    ]
    assert len(disconnect) == 1, (
        f"expected one ws_client_disconnected event; got {captured}"
    )
    assert disconnect[0]["client_id"] == "c-slow"
    assert disconnect[0]["reason"] == "slow consumer"


@pytest.mark.asyncio
async def test_healthy_client_unaffected_by_slow_peer():
    server = WebSocketServer()
    server._BROADCAST_PER_CLIENT_TIMEOUT_S = 0.2
    server._BROADCAST_BUDGET_S = 5.0
    slow_ws = _SlowSocket()
    healthy_ws = _HealthySocket()
    server._clients["c-slow"] = WebSocketClient(
        client_id="c-slow", websocket=slow_ws, client_type="chrome"
    )
    server._clients["c-fast"] = WebSocketClient(
        client_id="c-fast", websocket=healthy_ws, client_type="vscode"
    )

    sent = await server._broadcast(
        WSMessage(type="STATE_UPDATE", payload={"state": "FLOW"})
    )

    assert sent == 1, "healthy client should be counted as sent"
    assert "c-slow" not in server._clients
    assert "c-fast" in server._clients
    assert len(healthy_ws.sent) == 1


@pytest.mark.asyncio
async def test_reconnection_cycle_after_slow_close():
    """After a slow client is closed and removed, a fresh connection
    with the same client_id (after reconnect) must be allowed. The
    original socket must have received the explicit close frame."""
    server = WebSocketServer()
    server._BROADCAST_PER_CLIENT_TIMEOUT_S = 0.2
    server._BROADCAST_BUDGET_S = 5.0
    slow_ws = _SlowSocket()
    server._clients["c1"] = WebSocketClient(
        client_id="c1", websocket=slow_ws, client_type="chrome"
    )
    await server._broadcast(WSMessage(type="STATE_UPDATE", payload={}))
    assert "c1" not in server._clients
    # F22: the original (slow) socket must see the explicit close frame.
    assert slow_ws.closed_with == (1011, "slow consumer")

    # Now simulate reconnection — fresh client, fresh socket.
    new_ws = _HealthySocket()
    server._clients["c1"] = WebSocketClient(
        client_id="c1", websocket=new_ws, client_type="chrome"
    )
    sent = await server._broadcast(WSMessage(type="STATE_UPDATE", payload={}))
    assert sent == 1
    assert "c1" in server._clients


@pytest.mark.asyncio
async def test_close_on_already_dead_socket_does_not_raise():
    """F22: when the close frame can't be delivered (peer half-torn-down)
    the broadcast loop must swallow the exception and still remove the
    client. Pre-F22 the code never called close(), so this case was
    accidentally tolerated; the new code calls close() explicitly and
    must handle the raised exception."""
    server = WebSocketServer()
    server._BROADCAST_PER_CLIENT_TIMEOUT_S = 0.2
    server._BROADCAST_BUDGET_S = 5.0
    dead_ws = _AlreadyDeadSocket()
    server._clients["c-dead"] = WebSocketClient(
        client_id="c-dead", websocket=dead_ws, client_type="chrome"
    )

    # Must not raise even though close() throws.
    await server._broadcast(WSMessage(type="STATE_UPDATE", payload={}))
    assert "c-dead" not in server._clients

    # And exercise the helper directly — the close-on-dead-socket path
    # is the F22-specific contract we added.
    fresh_dead = _AlreadyDeadSocket()
    fresh_client = WebSocketClient(
        client_id="c2", websocket=fresh_dead, client_type="chrome"
    )
    # Must not raise.
    await server._close_slow_consumer(fresh_client, "test reason")
