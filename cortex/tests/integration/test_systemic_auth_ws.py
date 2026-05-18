"""Systemic capability-token AUTH-first handshake on the WebSocket
server (audit Debt-2, Commit 2).

The Wave-1 F07 fix gated only the destructive ``SHUTDOWN`` message. The
systemic close-out flips the default: every connection starts in
``pending_auth`` and the server refuses every type except ``AUTH``
until the client presents a valid token. Then the client receives an
``AUTH_OK`` ACK and any subsequent message is dispatched normally.

Five cases:

1. Connect + send a non-AUTH frame as the first message → close(1011)
   with ``EventType.AUTH_REJECTED`` logged.
2. Connect + send ``AUTH`` with the correct token → ``AUTH_OK`` reply,
   client flips ``authenticated=True``, further messages are honoured.
3. Connect + send ``AUTH`` with a wrong token → close(1011) with
   ``EventType.AUTH_REJECTED`` logged.
4. ``AUTH`` replay on an already-authenticated client → ACKed
   idempotently, no state regression (the connection is not bounced).
5. ``AUTH_REJECTED`` event carries the client id (and the cid if the
   inbound frame supplied one) so log aggregators can join on the
   correlation column.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from cortex.libs.auth.local_token import load_or_create_token
from cortex.libs.logging.structured import EventType
from cortex.libs.schemas.ws_message_types import MessageType
from cortex.services.api_gateway.websocket_server import (
    WebSocketClient,
    WebSocketServer,
)


class _FakeWS:
    """Records outbound sends and a close call so each test can assert
    the server emitted the right protocol frames without spinning up a
    real ``websockets.serve`` listener."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.close_called: tuple[int, str] | None = None
        self._open = True

    async def send(self, raw: str) -> None:
        if not self._open:
            raise RuntimeError("send on closed socket")
        self.sent.append(raw)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.close_called = (code, reason)
        self._open = False


@pytest.fixture()
def token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Provision a fresh auth-token file in ``tmp_path`` and return it."""
    target = tmp_path / "auth.token"
    monkeypatch.setattr(
        "cortex.libs.auth.local_token.auth_token_path", lambda: target
    )
    return load_or_create_token(target)


def _frame(type_: str, payload: dict[str, Any] | None = None,
           correlation_id: str | None = None) -> str:
    return json.dumps({
        "type": type_,
        "payload": payload or {},
        "timestamp": 0,
        "sequence": 0,
        "correlation_id": correlation_id,
    })


# ---------------------------------------------------------------------------
# Case 1: non-AUTH first frame → close(1011) + AUTH_REJECTED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_auth_first_frame_closes_connection(
    token: str, caplog: pytest.LogCaptureFixture,
) -> None:
    server = WebSocketServer()
    ws = _FakeWS()
    client = WebSocketClient(client_id="hostile", websocket=ws)

    with caplog.at_level(
        logging.WARNING,
        logger="cortex.services.api_gateway.websocket_server",
    ):
        await server._process_message(
            client, _frame(MessageType.IDENTIFY.value),
        )

    assert client.authenticated is False
    assert ws.close_called is not None
    code, reason = ws.close_called
    assert code == 1011
    assert "auth required" in reason
    rejection_lines = [
        rec for rec in caplog.records
        if EventType.AUTH_REJECTED.value in rec.getMessage()
    ]
    assert rejection_lines, (
        f"expected AUTH_REJECTED, saw {[r.getMessage() for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# Case 2: correct AUTH → AUTH_OK + further frames accepted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_handshake_unlocks_subsequent_messages(token: str) -> None:
    server = WebSocketServer()
    ws = _FakeWS()
    client = WebSocketClient(client_id="legit", websocket=ws)
    captured: list[dict[str, Any]] = []

    async def _on_user_action(payload: dict[str, Any]) -> None:
        captured.append(payload)

    server.set_user_action_callback(_on_user_action)

    # 1. AUTH handshake.
    await server._process_message(
        client,
        _frame(MessageType.AUTH.value, {"auth_token": token}),
    )
    assert client.authenticated is True
    # AUTH_OK should have been the first outbound frame.
    assert ws.sent, "expected AUTH_OK after successful AUTH"
    first = json.loads(ws.sent[0])
    assert first["type"] == MessageType.AUTH_OK.value
    assert ws.close_called is None

    # 2. Subsequent USER_ACTION is honoured.
    await server._process_message(
        client,
        _frame(
            MessageType.USER_ACTION.value,
            {"action": "engaged", "intervention_id": "iv_1"},
        ),
    )
    assert captured == [{"action": "engaged", "intervention_id": "iv_1"}]


# ---------------------------------------------------------------------------
# Case 3: wrong AUTH token → close(1011)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrong_auth_token_closes_connection(
    token: str, caplog: pytest.LogCaptureFixture,
) -> None:
    server = WebSocketServer()
    ws = _FakeWS()
    client = WebSocketClient(client_id="bad-token", websocket=ws)

    with caplog.at_level(
        logging.WARNING,
        logger="cortex.services.api_gateway.websocket_server",
    ):
        await server._process_message(
            client,
            _frame(MessageType.AUTH.value, {"auth_token": "0" * len(token)}),
        )

    assert client.authenticated is False
    assert ws.close_called is not None
    code, reason = ws.close_called
    assert code == 1011
    assert "invalid auth" in reason
    rejection_lines = [
        rec for rec in caplog.records
        if EventType.AUTH_REJECTED.value in rec.getMessage()
        and "invalid_token" in rec.getMessage()
    ]
    assert rejection_lines


# ---------------------------------------------------------------------------
# Case 4: AUTH replay is idempotent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_replay_is_idempotent(token: str) -> None:
    """A client that retries ``AUTH`` (e.g. after a transient reconnect)
    must not bounce itself out of an otherwise healthy session."""
    server = WebSocketServer()
    ws = _FakeWS()
    client = WebSocketClient(client_id="retrier", websocket=ws)

    await server._process_message(
        client,
        _frame(MessageType.AUTH.value, {"auth_token": token}),
    )
    assert client.authenticated is True
    first_sent_count = len(ws.sent)

    # Replay.
    await server._process_message(
        client,
        _frame(MessageType.AUTH.value, {"auth_token": token}),
    )
    # Still authenticated; the server re-ACKed.
    assert client.authenticated is True
    assert ws.close_called is None
    # We expect exactly one additional AUTH_OK on replay.
    assert len(ws.sent) == first_sent_count + 1
    second = json.loads(ws.sent[-1])
    assert second["type"] == MessageType.AUTH_OK.value


# ---------------------------------------------------------------------------
# Case 5: AUTH_REJECTED log carries the client id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_rejected_event_carries_client_id(
    token: str, caplog: pytest.LogCaptureFixture,
) -> None:
    server = WebSocketServer()
    ws = _FakeWS()
    client = WebSocketClient(client_id="trace_me", websocket=ws)

    with caplog.at_level(
        logging.WARNING,
        logger="cortex.services.api_gateway.websocket_server",
    ):
        await server._process_message(
            client,
            _frame(
                MessageType.STATE_UPDATE.value,  # any non-AUTH type
                {},
                correlation_id="cid_debt2_trace",
            ),
        )

    rejection_lines = [
        rec for rec in caplog.records
        if EventType.AUTH_REJECTED.value in rec.getMessage()
    ]
    assert rejection_lines
    msg = rejection_lines[0].getMessage()
    assert "client=trace_me" in msg
    assert "cid=cid_debt2_trace" in msg


# ---------------------------------------------------------------------------
# Adversarial smoke test: run a real listener and confirm it closes within 2s
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_listener_closes_unauthed_within_two_seconds(
    token: str,
) -> None:
    """Mirrors the manual adversarial test from the task spec.

    Starts a real WebSocket server, opens a real client, sends a
    non-AUTH frame as the first message, and asserts the connection
    closes within 2 s. Without the AUTH-first gate this test hangs
    until the test framework times out.
    """
    server = WebSocketServer()
    started = await server.start()
    if not started:
        pytest.skip("WebSocket server failed to start (port in use?)")

    try:
        import websockets

        async with websockets.connect(
            f"ws://{server._config.host}:{server._config.ws_port}",
        ) as ws:
            await ws.send(_frame(MessageType.IDENTIFY.value, {"client_type": "x"}))
            with pytest.raises(websockets.ConnectionClosed):
                # The server should close us in well under 2s; recv waits
                # for the close frame.
                await asyncio.wait_for(ws.recv(), timeout=2.0)
    finally:
        await server.stop()
