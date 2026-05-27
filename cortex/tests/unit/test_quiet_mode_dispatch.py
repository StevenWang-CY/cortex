"""P0 §3.11 — WebSocket dispatch of QUIET_MODE_TOGGLE / SNOOZE_REQUEST.

Unit-level coverage of the validation + forward path; the daemon-side
behaviour lives in ``test_set_quiet_mode.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from cortex.libs.schemas.ws_message import WSMessage
from cortex.libs.schemas.ws_message_types import MessageType
from cortex.services.api_gateway.websocket_server import (
    WebSocketServer,
)


class _StubClient:
    """Minimal WebSocketClient stub — only the fields the handlers read."""

    def __init__(self) -> None:
        self.client_id = "test"
        self.client_type = "desktop"
        self.authenticated = True
        # ``websocket`` is unused by the handlers under test.
        self.websocket = None  # type: ignore[assignment]


@pytest.fixture()
def server() -> WebSocketServer:
    return WebSocketServer()


@pytest.mark.asyncio
async def test_quiet_mode_toggle_forwards_valid_kind(server: WebSocketServer) -> None:
    captured: list[tuple[str, int | None, str]] = []

    async def cb(kind: str, duration: int | None, source: str) -> None:
        captured.append((kind, duration, source))

    server.set_quiet_mode_toggle_callback(cb)
    msg = WSMessage(
        type=MessageType.QUIET_MODE_TOGGLE,
        payload={"kind": "snooze_15", "duration_minutes": 15, "source": "popup"},
    )
    await server._handle_quiet_mode_toggle(_StubClient(), msg)  # type: ignore[arg-type]
    assert captured == [("snooze_15", 15, "popup")]


@pytest.mark.asyncio
async def test_quiet_mode_toggle_rejects_invalid_kind(server: WebSocketServer) -> None:
    captured: list[Any] = []
    server.set_quiet_mode_toggle_callback(lambda *a: captured.append(a))
    await server._handle_quiet_mode_toggle(
        _StubClient(),  # type: ignore[arg-type]
        WSMessage(
            type=MessageType.QUIET_MODE_TOGGLE,
            payload={"kind": "yolo", "duration_minutes": 5},
        ),
    )
    assert captured == []


@pytest.mark.asyncio
async def test_quiet_mode_toggle_clamps_duration(server: WebSocketServer) -> None:
    captured: list[tuple[str, int | None, str]] = []

    async def cb(kind: str, duration: int | None, source: str) -> None:
        captured.append((kind, duration, source))

    server.set_quiet_mode_toggle_callback(cb)
    await server._handle_quiet_mode_toggle(
        _StubClient(),  # type: ignore[arg-type]
        WSMessage(
            type=MessageType.QUIET_MODE_TOGGLE,
            payload={
                "kind": "quiet_session",
                "duration_minutes": 99999,
                # Phase-3 / Audit-1.5 P1-2: unknown sources are
                # replaced with the client's identified type (desktop)
                # instead of forwarded verbatim, so analytics never
                # see attacker-controlled junk strings.
                "source": "x",
            },
        ),
    )
    assert captured == [("quiet_session", 240, "desktop")]


@pytest.mark.asyncio
async def test_quiet_mode_toggle_off_clears(server: WebSocketServer) -> None:
    captured: list[tuple[str, int | None, str]] = []

    async def cb(kind: str, duration: int | None, source: str) -> None:
        captured.append((kind, duration, source))

    server.set_quiet_mode_toggle_callback(cb)
    await server._handle_quiet_mode_toggle(
        _StubClient(),  # type: ignore[arg-type]
        WSMessage(
            type=MessageType.QUIET_MODE_TOGGLE,
            payload={"kind": "off"},
        ),
    )
    assert captured == [("off", None, "desktop")]


@pytest.mark.asyncio
async def test_snooze_request_defaults_to_15(server: WebSocketServer) -> None:
    captured: list[tuple[str, int | None, str]] = []

    async def cb(kind: str, duration: int | None, source: str) -> None:
        captured.append((kind, duration, source))

    server.set_quiet_mode_toggle_callback(cb)
    await server._handle_snooze_request(
        _StubClient(),  # type: ignore[arg-type]
        WSMessage(type=MessageType.SNOOZE_REQUEST, payload={}),
    )
    assert captured == [("snooze_15", 15, "desktop")]


@pytest.mark.asyncio
async def test_snooze_request_honours_duration(server: WebSocketServer) -> None:
    captured: list[tuple[str, int | None, str]] = []

    async def cb(kind: str, duration: int | None, source: str) -> None:
        captured.append((kind, duration, source))

    server.set_quiet_mode_toggle_callback(cb)
    await server._handle_snooze_request(
        _StubClient(),  # type: ignore[arg-type]
        WSMessage(
            type=MessageType.SNOOZE_REQUEST,
            payload={"duration_minutes": 5, "source": "vscode"},
        ),
    )
    assert captured == [("snooze_15", 5, "vscode")]
