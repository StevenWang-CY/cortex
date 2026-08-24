"""Protocol negotiation, event identity, and clock-domain compatibility."""

from __future__ import annotations

import json
from typing import Any

import pytest

from cortex.application.clock import FakeClock
from cortex.libs.schemas.api import AckResponse
from cortex.libs.schemas.protocol import negotiate_protocol
from cortex.libs.schemas.ws_message import WSMessage
from cortex.libs.schemas.ws_message_types import MessageType
from cortex.services.api_gateway import websocket_server as ws_module
from cortex.services.api_gateway.websocket_server import (
    WebSocketClient,
    WebSocketServer,
)


class _Socket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed: tuple[int, str] | None = None

    async def send(self, raw: str) -> None:
        self.sent.append(raw)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)


def test_minor_versions_negotiate_down_without_crossing_major() -> None:
    assert negotiate_protocol(["2.9"]) == "2.0"
    assert negotiate_protocol(["1.8"]) == "1.0"
    assert negotiate_protocol(["3.0"]) is None
    assert negotiate_protocol(["not-a-version"]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("offer", "selected"),
    [
        ({}, "1.0"),
        ({"protocol_version": "1.7"}, "1.0"),
        ({"protocol_version": "2.8"}, "2.0"),
    ],
)
async def test_auth_negotiates_legacy_and_new_minors(
    monkeypatch: pytest.MonkeyPatch,
    offer: dict[str, Any],
    selected: str,
) -> None:
    monkeypatch.setattr(ws_module, "verify_token", lambda _token: True)
    clock = FakeClock(wall_unix_ms=1_700_000_000_000, mono_ns=42)
    server = WebSocketServer(clock=clock)
    socket = _Socket()
    client = WebSocketClient(client_id="client", websocket=socket)
    auth = WSMessage.from_clock(
        clock=clock,
        type=MessageType.AUTH,
        payload={"auth_token": "valid", **offer},
    )

    await server._process_message(client, auth.to_json())

    assert client.authenticated is True
    assert client.protocol_version == selected
    response = WSMessage.from_json(socket.sent[-1])
    assert response.type == MessageType.AUTH_OK.value
    assert response.protocol_version == selected
    assert response.payload["selected_protocol_version"] == selected
    assert response.boot_id == clock.boot_id


@pytest.mark.asyncio
async def test_auth_rejects_unknown_major_with_typed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ws_module, "verify_token", lambda _token: True)
    clock = FakeClock(wall_unix_ms=10, mono_ns=20)
    server = WebSocketServer(clock=clock)
    socket = _Socket()
    client = WebSocketClient(client_id="client", websocket=socket)
    auth = WSMessage.from_clock(
        clock=clock,
        type=MessageType.AUTH,
        payload={"auth_token": "valid", "protocol_version": "3.0"},
    )

    await server._process_message(client, auth.to_json())

    assert client.authenticated is False
    error = WSMessage.from_json(socket.sent[-1])
    assert error.type == MessageType.PROTOCOL_ERROR.value
    assert error.payload["code"] == "unsupported_protocol"
    assert socket.closed == (1002, "unsupported protocol")


@pytest.mark.asyncio
async def test_v2_duplicate_event_is_dispatched_once() -> None:
    clock = FakeClock(wall_unix_ms=10, mono_ns=20)
    server = WebSocketServer(clock=clock)
    socket = _Socket()
    client = WebSocketClient(
        client_id="client",
        websocket=socket,
        authenticated=True,
        protocol_version="2.0",
    )
    applied: list[dict[str, Any]] = []
    server.set_settings_callback(lambda payload: applied.append(payload))
    event = WSMessage.from_clock(
        clock=clock,
        type=MessageType.SETTINGS_SYNC,
        payload={"settings_version": 1, "quiet_mode": True},
        sequence=1,
    )
    raw = event.to_json()

    await server._process_message(client, raw)
    await server._process_message(client, raw)

    assert applied == [{"settings_version": 1, "quiet_mode": True}]
    assert len(client.seen_event_ids) == 1


def test_legacy_envelope_never_invents_monotonic_provenance() -> None:
    legacy = WSMessage.from_json(json.dumps({
        "type": "STATE_UPDATE",
        "payload": {},
        "timestamp": 123.5,
    }))
    assert legacy.schema_version == "1.0"
    assert legacy.protocol_version == "1.0"
    assert legacy.sent_at_unix_ms == 123_500
    assert legacy.sent_at_mono_ns == 0
    assert legacy.boot_id.int == 0


def test_partial_dual_clock_metadata_fails_closed() -> None:
    with pytest.raises(ValueError, match="must be supplied together"):
        AckResponse(observed_at_unix_ms=1)

    with pytest.raises(ValueError, match="must be supplied together"):
        WSMessage(type=MessageType.SETTINGS_SYNC, sent_at_unix_ms=1)
