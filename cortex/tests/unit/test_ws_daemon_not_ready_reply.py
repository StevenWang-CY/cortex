"""P2-22: _handle_force_recap / _handle_dismiss_overlay / _handle_goal_set /
_handle_test_provider send an ERROR frame with code='daemon_not_ready'
when ``_resolve_daemon()`` returns None.

Strategy
--------
* Patch ``WebSocketServer._resolve_daemon`` to return ``None``.
* Create a fake WebSocket that captures ``send()`` calls.
* Call each handler directly.
* Assert the captured frame's ``type == "ERROR"`` and
  ``payload.code == "daemon_not_ready"``.
* Verify the ``correlation_id`` from the request is echoed back.

``_handle_cost_request`` is EXCLUDED (owned by Agent C) per the task
spec. We test ``FORCE_RECAP``, ``DISMISS_OVERLAY``, ``GOAL_SET``, and
``TEST_PROVIDER``.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from cortex.libs.schemas.ws_message_types import MessageType
from cortex.services.api_gateway.websocket_server import WebSocketClient, WebSocketServer, WSMessage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client_capturing() -> tuple[WebSocketClient, list[str]]:
    """Return a WebSocketClient whose websocket captures sent frames."""
    frames: list[str] = []

    class _CapturingSock:
        async def send(self, raw: str) -> None:
            frames.append(raw)

    client = WebSocketClient(
        client_id="test-p2-22",
        websocket=_CapturingSock(),
        client_type="chrome",
    )
    return client, frames


def _msg(type_: str, *, cid: str | None = "cid_test_1234") -> WSMessage:
    return WSMessage(
        type=type_,
        payload={},
        correlation_id=cid,
    )


async def _call_with_no_daemon(handler_name: str, msg: WSMessage) -> list[str]:
    """Run handler under ``_resolve_daemon → None`` and return sent frames."""
    server = WebSocketServer()
    client, frames = _make_client_capturing()
    server._clients[client.client_id] = client

    with patch.object(server, "_resolve_daemon", return_value=None):
        handler = getattr(server, handler_name)
        await handler(client, msg)

    return frames


# ---------------------------------------------------------------------------
# FORCE_RECAP
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_force_recap_daemon_not_ready_sends_error() -> None:
    frames = await _call_with_no_daemon(
        "_handle_force_recap", _msg(MessageType.FORCE_RECAP.value)
    )
    assert len(frames) == 1, f"Expected 1 ERROR frame, got {len(frames)}"
    parsed = json.loads(frames[0])
    assert parsed["type"] == "ERROR"
    assert parsed["payload"]["code"] == "daemon_not_ready"


@pytest.mark.asyncio
async def test_force_recap_daemon_not_ready_echoes_cid() -> None:
    frames = await _call_with_no_daemon(
        "_handle_force_recap",
        _msg(MessageType.FORCE_RECAP.value, cid="cid_force_recap_test"),
    )
    parsed = json.loads(frames[0])
    assert parsed["payload"]["correlation_id"] == "cid_force_recap_test"


# ---------------------------------------------------------------------------
# DISMISS_OVERLAY
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dismiss_overlay_daemon_not_ready_sends_error() -> None:
    frames = await _call_with_no_daemon(
        "_handle_dismiss_overlay", _msg(MessageType.DISMISS_OVERLAY.value)
    )
    assert len(frames) == 1
    parsed = json.loads(frames[0])
    assert parsed["type"] == "ERROR"
    assert parsed["payload"]["code"] == "daemon_not_ready"


# ---------------------------------------------------------------------------
# GOAL_SET
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_goal_set_daemon_not_ready_sends_error() -> None:
    frames = await _call_with_no_daemon(
        "_handle_goal_set", _msg(MessageType.GOAL_SET.value)
    )
    assert len(frames) == 1
    parsed = json.loads(frames[0])
    assert parsed["type"] == "ERROR"
    assert parsed["payload"]["code"] == "daemon_not_ready"


# ---------------------------------------------------------------------------
# TEST_PROVIDER
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_test_provider_daemon_not_ready_sends_error() -> None:
    frames = await _call_with_no_daemon(
        "_handle_test_provider", _msg(MessageType.TEST_PROVIDER.value)
    )
    assert len(frames) == 1
    parsed = json.loads(frames[0])
    assert parsed["type"] == "ERROR"
    assert parsed["payload"]["code"] == "daemon_not_ready"


# ---------------------------------------------------------------------------
# Verify cid is None when msg has no correlation_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_frame_cid_none_when_no_cid() -> None:
    frames = await _call_with_no_daemon(
        "_handle_force_recap",
        _msg(MessageType.FORCE_RECAP.value, cid=None),
    )
    parsed = json.loads(frames[0])
    assert parsed["payload"]["correlation_id"] is None
