"""F23 — Pending correlation-id futures cancelled on client disconnect.

WebSocketServer.request_context registers a future keyed by
correlation_id; the client's CONTEXT_RESPONSE resolves it. Pre-F23,
if the client disconnected before responding, the future hung until
the per-call timeout (5 s) elapsed — every concurrent caller paid the
latency. F23 tracks ``client_id → {correlation_id}`` so disconnect
cancels every in-flight future for that client.

Run with: ``pytest cortex/tests/unit/test_pending_context_cleanup.py``
"""

from __future__ import annotations

import asyncio

import pytest

from cortex.services.api_gateway.websocket_server import (
    WebSocketClient,
    WebSocketServer,
    WSMessage,
)


class _FakeWebSocket:
    """Minimal stub: ``send`` records, ``close`` no-ops."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        return None


@pytest.mark.asyncio
async def test_disconnect_with_no_pending_is_noop():
    server = WebSocketServer()
    server._clients["c1"] = WebSocketClient(
        client_id="c1", websocket=_FakeWebSocket(), client_type="chrome",
    )
    # No pending requests; cancellation must return 0 and not raise.
    cancelled = server._cancel_pending_for_client("c1")
    assert cancelled == 0
    assert "c1" not in server._pending_cids_by_client


@pytest.mark.asyncio
async def test_disconnect_with_pending_cancels_futures():
    server = WebSocketServer()
    ws = _FakeWebSocket()
    server._clients["c1"] = WebSocketClient(
        client_id="c1", websocket=ws, client_type="chrome",
    )

    # Kick off two concurrent context requests against the same client.
    task_a = asyncio.create_task(
        server.request_context("chrome", timeout=10.0)
    )
    task_b = asyncio.create_task(
        server.request_context("chrome", timeout=10.0)
    )
    # Yield so request_context registers its future.
    await asyncio.sleep(0.05)

    pending = server._pending_cids_by_client.get("c1") or set()
    assert len(pending) == 2, (
        f"expected two pending cids; got {pending}"
    )

    # Simulate disconnect.
    cancelled = server._cancel_pending_for_client("c1")
    assert cancelled == 2

    result_a = await task_a
    result_b = await task_b
    # Both calls returned empty (cancelled → fallback to {}) — no hang.
    assert result_a == {}
    assert result_b == {}
    assert "c1" not in server._pending_cids_by_client
    assert not server._pending_context_requests


@pytest.mark.asyncio
async def test_reconnect_issues_fresh_cid():
    """A fresh client (same client_id, new socket) must not inherit the
    cancelled futures of the disconnected predecessor."""
    server = WebSocketServer()
    ws1 = _FakeWebSocket()
    server._clients["c1"] = WebSocketClient(
        client_id="c1", websocket=ws1, client_type="chrome",
    )

    task = asyncio.create_task(server.request_context("chrome", timeout=10.0))
    await asyncio.sleep(0.05)
    # Capture the cid before disconnect.
    first_cids = list(server._pending_cids_by_client.get("c1") or set())
    assert len(first_cids) == 1

    server._cancel_pending_for_client("c1")
    await task

    # Reconnect.
    ws2 = _FakeWebSocket()
    server._clients["c1"] = WebSocketClient(
        client_id="c1", websocket=ws2, client_type="chrome",
    )
    task2 = asyncio.create_task(server.request_context("chrome", timeout=10.0))
    await asyncio.sleep(0.05)
    second_cids = list(server._pending_cids_by_client.get("c1") or set())
    assert len(second_cids) == 1
    assert second_cids[0] != first_cids[0], (
        "fresh cid expected after reconnect"
    )

    # Resolve cleanly via a CONTEXT_RESPONSE so the test doesn't leak.
    server._handle_context_response(
        WSMessage(
            type="CONTEXT_RESPONSE",
            payload={"ok": True},
            correlation_id=second_cids[0],
        )
    )
    result = await task2
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_concurrent_disconnect_and_response_no_crash():
    """If a CONTEXT_RESPONSE races a disconnect, the future should be
    resolved exactly once and neither path raises."""
    server = WebSocketServer()
    ws = _FakeWebSocket()
    server._clients["c1"] = WebSocketClient(
        client_id="c1", websocket=ws, client_type="chrome",
    )

    task = asyncio.create_task(server.request_context("chrome", timeout=10.0))
    await asyncio.sleep(0.05)
    cid = next(iter(server._pending_cids_by_client["c1"]))

    # Resolve via CONTEXT_RESPONSE first.
    server._handle_context_response(
        WSMessage(
            type="CONTEXT_RESPONSE",
            payload={"value": 42},
            correlation_id=cid,
        )
    )
    # Now the disconnect arrives — should be a no-op for this cid since
    # the future is already resolved and removed.
    cancelled = server._cancel_pending_for_client("c1")
    assert cancelled == 0

    result = await task
    assert result == {"value": 42}
