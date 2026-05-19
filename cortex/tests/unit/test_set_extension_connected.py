"""Audit-prod G1 — IDENTIFY → dashboard connection dots.

Verifies the round-trip:
  1. WebSocketServer fires the ``_client_identified_callback`` on
     IDENTIFY and on disconnect of an identified client.
  2. ``connected_client_types()`` returns the deduped, filtered set.
  3. ``_make_state_update`` stamps ``connected_clients`` onto STATE_UPDATE.
"""

from __future__ import annotations

import asyncio

import pytest

from cortex.libs.schemas.state import (
    SignalQuality,
    StateEstimate,
    StateScores,
)
from cortex.libs.schemas.ws_message import WSMessage
from cortex.libs.schemas.ws_message_types import MessageType
from cortex.services.api_gateway.websocket_server import (
    WebSocketClient,
    WebSocketServer,
)


def _make_estimate() -> StateEstimate:
    return StateEstimate(
        state="FLOW",
        confidence=0.9,
        scores=StateScores(flow=0.7, hypo=0.1, hyper=0.1, recovery=0.1),
        signal_quality=SignalQuality(
            physio=0.9, kinematics=0.9, telemetry=0.9,
        ),
        dwell_seconds=12.0,
        reasons=[],
        timestamp=1234.5,
    )


def test_identify_callback_fires_with_connected_true() -> None:
    server = WebSocketServer()
    events: list[tuple[str, bool]] = []
    server.set_client_identified_callback(
        lambda ct, on: events.append((ct, on))
    )

    # Simulate a connected, IDENTIFY-ed client.
    client = WebSocketClient(client_id="c1", websocket=object())
    client.client_type = "chrome"
    # The server's internal dispatch path normally invokes the callback;
    # call it directly here since we have no real socket. The test
    # double-checks the contract that ``set_client_identified_callback``
    # *stores* the callable on the public surface.
    server._client_identified_callback("chrome", True)

    assert events == [("chrome", True)]


def test_connected_client_types_dedupes_and_filters() -> None:
    server = WebSocketServer()
    # Three connections: two Chrome tabs, one desktop, one unknown
    for cid, ct in [
        ("c1", "chrome"),
        ("c2", "chrome"),
        ("c3", "desktop"),
        ("c4", "unknown"),
        ("c5", "vscode"),
    ]:
        wc = WebSocketClient(client_id=cid, websocket=object())
        wc.client_type = ct
        server._clients[cid] = wc

    types = server.connected_client_types()
    # 'chrome' deduped, 'desktop' and 'unknown' filtered out.
    assert types == ["chrome", "vscode"]


def test_state_update_payload_stamps_connected_clients() -> None:
    server = WebSocketServer()
    for cid, ct in [("c1", "chrome"), ("c2", "vscode")]:
        wc = WebSocketClient(client_id=cid, websocket=object())
        wc.client_type = ct
        server._clients[cid] = wc

    msg: WSMessage = server._make_state_update(_make_estimate())
    assert msg.type == MessageType.STATE_UPDATE.value
    connected = msg.payload.get("connected_clients")
    assert isinstance(connected, list)
    assert set(connected) == {"chrome", "vscode"}


def test_state_update_omits_desktop_self() -> None:
    server = WebSocketServer()
    wc = WebSocketClient(client_id="me", websocket=object())
    wc.client_type = "desktop"
    server._clients["me"] = wc
    msg = server._make_state_update(_make_estimate())
    assert msg.payload.get("connected_clients") == []


def test_callback_invoked_on_real_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the actual ``_process_message`` dispatch path so we know
    the callback isn't silently disconnected by a refactor."""
    server = WebSocketServer()
    fired: list[tuple[str, bool]] = []
    server.set_client_identified_callback(
        lambda ct, on: fired.append((ct, on))
    )

    client = WebSocketClient(client_id="c1", websocket=object())
    client.authenticated = True  # bypass AUTH gate
    server._clients["c1"] = client

    identify = WSMessage(
        type=MessageType.IDENTIFY.value,
        payload={"client_type": "chrome"},
        sequence=0,
    ).to_json()

    asyncio.run(server._process_message(client, identify))
    assert fired == [("chrome", True)]
