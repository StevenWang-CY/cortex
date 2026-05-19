"""Audit F07 — local capability token tests.

Covers the token-file module itself plus the WebSocket SHUTDOWN gate
that depends on it. Each test fails on ``main`` because the module did
not exist there; together they prove (a) the token is provisioned with
mode 0600 atomically, (b) ``verify_token`` is constant-time and accepts
only an exact match, (c) a SHUTDOWN message without the token does not
invoke the shutdown callback.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from cortex.libs.auth.local_token import (
    load_or_create_token,
    verify_token,
)
from cortex.services.api_gateway.websocket_server import (
    WebSocketClient,
    WebSocketServer,
)


def test_load_or_create_token_is_idempotent(tmp_path: Path) -> None:
    token_file = tmp_path / "auth.token"
    first = load_or_create_token(token_file)
    second = load_or_create_token(token_file)
    assert first == second
    assert token_file.exists()


def test_token_file_is_user_only_readable(tmp_path: Path) -> None:
    if sys.platform.startswith("win"):
        pytest.skip("POSIX permission semantics do not apply on Windows")
    token_file = tmp_path / "auth.token"
    load_or_create_token(token_file)
    mode = stat.S_IMODE(os.stat(token_file).st_mode)
    # 0o600: read+write owner, nothing else.
    assert mode == 0o600, f"expected 0o600 got 0o{mode:o}"


def test_verify_token_rejects_missing(tmp_path: Path) -> None:
    token_file = tmp_path / "auth.token"
    load_or_create_token(token_file)
    assert verify_token(None, path=token_file) is False
    assert verify_token("", path=token_file) is False


def test_verify_token_rejects_wrong(tmp_path: Path) -> None:
    token_file = tmp_path / "auth.token"
    load_or_create_token(token_file)
    assert verify_token("definitely-not-the-real-token", path=token_file) is False


def test_verify_token_accepts_correct(tmp_path: Path) -> None:
    token_file = tmp_path / "auth.token"
    token = load_or_create_token(token_file)
    assert verify_token(token, path=token_file) is True


def test_load_or_create_token_replaces_truncated_file(tmp_path: Path) -> None:
    token_file = tmp_path / "auth.token"
    token_file.write_text("short", encoding="utf-8")
    rebuilt = load_or_create_token(token_file)
    assert rebuilt != "short"
    assert len(rebuilt) >= 32


@pytest.mark.asyncio
async def test_ws_shutdown_rejects_unauthenticated_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the auth token, SHUTDOWN must not invoke the callback."""
    token_file = tmp_path / "auth.token"
    monkeypatch.setattr(
        "cortex.libs.auth.local_token.auth_token_path", lambda: token_file
    )
    load_or_create_token(token_file)

    server = WebSocketServer()
    shutdown_called = asyncio.Event()

    async def _cb() -> None:
        shutdown_called.set()

    server.set_shutdown_callback(_cb)

    class _FakeWS:
        async def send(self, raw: str) -> None:
            return None

    client = WebSocketClient(client_id="malicious", websocket=_FakeWS())
    payload = {"type": "SHUTDOWN", "payload": {}, "timestamp": 0, "sequence": 0}
    await server._process_message(client, json.dumps(payload))

    # 50ms grace — if the callback were ever going to fire, it would by now.
    await asyncio.sleep(0.05)
    assert not shutdown_called.is_set(), "SHUTDOWN without token should be rejected"


@pytest.mark.asyncio
async def test_ws_shutdown_accepts_correct_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the matching auth token, SHUTDOWN must invoke the callback.

    Debt-2 (audit): the systemic AUTH-first gate runs before the
    SHUTDOWN handler can ever dispatch, so we AUTH the client first.
    The legacy F07 inline check is preserved as defense-in-depth and
    is exercised separately by ``test_ws_shutdown_rejects_unauthenticated_message``.
    """
    token_file = tmp_path / "auth.token"
    monkeypatch.setattr(
        "cortex.libs.auth.local_token.auth_token_path", lambda: token_file
    )
    token = load_or_create_token(token_file)

    server = WebSocketServer()
    shutdown_called = asyncio.Event()

    async def _cb() -> None:
        shutdown_called.set()

    server.set_shutdown_callback(_cb)

    class _FakeWS:
        async def send(self, raw: str) -> None:
            return None

        async def close(self, code: int = 1000, reason: str = "") -> None:
            return None

    client = WebSocketClient(client_id="legit", websocket=_FakeWS())
    # Debt-2 prerequisite: AUTH before any other frame.
    await server._process_message(
        client,
        json.dumps({
            "type": "AUTH",
            "payload": {"auth_token": token},
            "timestamp": 0,
            "sequence": 0,
        }),
    )
    assert client.authenticated is True

    payload = {
        "type": "SHUTDOWN",
        "payload": {"auth_token": token},
        "timestamp": 0,
        "sequence": 0,
    }
    await server._process_message(client, json.dumps(payload))
    await asyncio.wait_for(shutdown_called.wait(), timeout=1.0)
    assert shutdown_called.is_set()
