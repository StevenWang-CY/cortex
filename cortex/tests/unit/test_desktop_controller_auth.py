"""Desktop-shell client-side capability-token plumbing (audit Debt-2,
Commit 3).

The desktop_shell's WS-mode client (``WebSocketBridge`` in
``cortex.apps.desktop_shell.main``) speaks the same protocol the
browser extension does. With the systemic AUTH-first gate landed
in Commit 2, this client must:

1. Load the capability token at startup via
   ``cortex.libs.auth.load_or_create_token`` and cache it.
2. Send an ``AUTH`` frame as the FIRST message after every successful
   ``websockets.connect`` (before ``IDENTIFY``).
3. Expose a ``refresh_auth_token`` entry point for the Settings panel
   "Rotate authentication token" affordance (Commit 5).

Two cases:

* ``test_bridge_caches_token_at_init`` — the bridge reads the token
  file on construction; this is the contract the Settings rotation
  path leans on so the cache is in lockstep with disk.
* ``test_bridge_sends_auth_first_after_connect`` — the connect loop
  emits an ``AUTH`` frame BEFORE ``IDENTIFY``, with the cached token
  in the payload. The daemon-side gate in Commit 2 requires this
  ordering.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture()
def auth_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Provision a fresh capability token in ``tmp_path``."""
    from cortex.libs.auth.local_token import load_or_create_token

    target = tmp_path / "auth.token"
    monkeypatch.setattr(
        "cortex.libs.auth.local_token.auth_token_path", lambda: target
    )
    return load_or_create_token(target)


@pytest.fixture()
def WebSocketBridge() -> Any:
    """Load the real ``WebSocketBridge`` against the offscreen Qt platform.

    PySide6 is a project dependency; running with ``QT_QPA_PLATFORM=offscreen``
    keeps the test headless without stubbing every imported widget.
    """
    from cortex.apps.desktop_shell.main import WebSocketBridge as Bridge
    return Bridge


def test_bridge_caches_token_at_init(
    auth_token: str, WebSocketBridge: Any,
) -> None:
    """Case 1: the bridge reads the token file on construction.

    Settings → "Rotate authentication token" relies on this so the in-
    memory cache equals the on-disk value the moment any reconnect
    fires the AUTH frame.
    """
    bridge = WebSocketBridge(host="127.0.0.1", port=9473)
    assert bridge._auth_token == auth_token


@pytest.mark.asyncio
async def test_bridge_sends_auth_first_after_connect(
    auth_token: str, WebSocketBridge: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case 2: the connect loop sends ``AUTH`` BEFORE ``IDENTIFY``.

    We replace ``websockets.connect`` with a fake context manager that
    captures every outbound frame, flip ``_running`` False as soon as
    the handshake has happened, and assert the first send was AUTH
    with the cached token and the second was IDENTIFY.
    """
    sent: list[str] = []

    bridge = WebSocketBridge(host="127.0.0.1", port=9473)

    class _FakeWS:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def send(self, raw: str) -> None:
            sent.append(raw)

        def __aiter__(self) -> Any:
            return self

        async def __anext__(self) -> str:
            # Stop the outer connect loop now that the handshake is on
            # the wire. ``async for`` exits on StopAsyncIteration and
            # the ``while self._running`` guard then breaks the loop.
            bridge._running = False
            raise StopAsyncIteration

        async def close(self) -> None:
            return None

    class _FakeWebsocketsModule:
        @staticmethod
        def connect(uri: str) -> Any:  # noqa: ARG004 — uri unused in test
            return _FakeWS()

    monkeypatch.setitem(sys.modules, "websockets", _FakeWebsocketsModule)

    bridge._running = True
    await asyncio.wait_for(bridge._connect_loop(), timeout=2.0)

    assert len(sent) >= 2, f"expected AUTH + IDENTIFY, got {sent}"

    first = json.loads(sent[0])
    assert first["type"] == "AUTH"
    assert first["payload"]["auth_token"] == auth_token

    second = json.loads(sent[1])
    assert second["type"] == "IDENTIFY"
    assert second["payload"]["client_type"] == "desktop"
