"""Audit F19 — end-to-end correlation IDs.

Asserts that:

1. The correlation ``ContextVar`` round-trips through ``correlation_scope``.
2. ``WSMessage`` builders emit the active correlation id without callers
   threading it explicitly.
3. The FastAPI middleware mints a fresh id when no header is present and
   echoes back the caller-supplied id when one is.

Each test in this module fails on ``main`` before the F19 commit; they
pass after.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from cortex.libs.logging.correlation import (
    correlation_scope,
    get_correlation_id,
    new_correlation_id,
)
from cortex.services.api_gateway.app import create_app
from cortex.services.api_gateway.websocket_server import WebSocketServer, WSMessage


def test_correlation_scope_round_trip() -> None:
    assert get_correlation_id() is None
    with correlation_scope() as cid:
        assert cid.startswith("cid_")
        assert get_correlation_id() == cid
    assert get_correlation_id() is None


def test_correlation_scope_honours_supplied_id() -> None:
    supplied = "cid_deadbeef0000"
    with correlation_scope(supplied) as cid:
        assert cid == supplied
        assert get_correlation_id() == supplied


def test_correlation_scope_nests_cleanly() -> None:
    outer = new_correlation_id()
    inner = new_correlation_id()
    assert outer != inner
    with correlation_scope(outer):
        assert get_correlation_id() == outer
        with correlation_scope(inner):
            assert get_correlation_id() == inner
        # Inner scope restored the outer binding rather than clearing it.
        assert get_correlation_id() == outer
    assert get_correlation_id() is None


def test_http_middleware_mints_id_when_absent() -> None:
    app = create_app()
    client = TestClient(app)
    response = client.get("/health")
    cid = response.headers.get("X-Cortex-Request-ID")
    assert cid is not None
    assert cid.startswith("cid_")


def test_http_middleware_echoes_supplied_id() -> None:
    supplied = "cid_111122223333"
    app = create_app()
    client = TestClient(app)
    response = client.get("/health", headers={"X-Cortex-Request-ID": supplied})
    assert response.headers.get("X-Cortex-Request-ID") == supplied


def test_correlation_cleared_after_request() -> None:
    """Middleware must restore the empty contextvar after the response."""
    app = create_app()
    client = TestClient(app)
    client.get("/health")
    assert get_correlation_id() is None


@pytest.mark.asyncio
async def test_broadcast_stamps_active_correlation_id_on_outgoing_message() -> None:
    """A WSMessage with no correlation_id picks up the current scope's id
    when _broadcast runs. Verified by snooping a fake client websocket."""
    server = WebSocketServer()

    sent_payloads: list[str] = []

    class _FakeWebSocket:
        async def send(self, raw: str) -> None:
            sent_payloads.append(raw)

    fake = _FakeWebSocket()
    server._clients["fake"] = type(
        "C",
        (),
        {"websocket": fake, "client_type": "test", "client_id": "fake"},
    )()

    msg = WSMessage(type="STATE_UPDATE", payload={"state": "FLOW"})
    assert msg.correlation_id is None

    with correlation_scope("cid_aaaabbbbcccc"):
        await server._broadcast(msg)

    assert len(sent_payloads) == 1
    parsed = json.loads(sent_payloads[0])
    assert parsed["correlation_id"] == "cid_aaaabbbbcccc"


@pytest.mark.asyncio
async def test_broadcast_preserves_caller_supplied_id() -> None:
    """If the caller already set msg.correlation_id, _broadcast does not
    overwrite it. Important for the apply_intervention path where the
    HTTP middleware id flows through to the WS message."""
    server = WebSocketServer()
    sent: list[str] = []

    class _FakeWebSocket:
        async def send(self, raw: str) -> None:
            sent.append(raw)

    server._clients["fake"] = type(
        "C",
        (),
        {"websocket": _FakeWebSocket(), "client_type": "test", "client_id": "fake"},
    )()

    msg = WSMessage(
        type="STATE_UPDATE",
        payload={"state": "FLOW"},
        correlation_id="cid_supplied00000",
    )
    with correlation_scope("cid_othervalue000"):
        await server._broadcast(msg)

    parsed = json.loads(sent[0])
    assert parsed["correlation_id"] == "cid_supplied00000"
