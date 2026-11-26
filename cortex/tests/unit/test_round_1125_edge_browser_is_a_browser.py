"""Microsoft Edge runs the same extension and must get the browser features.

The extension identifies itself as ``client_type="edge"`` on Edge
(``lib/browser.ts::detectBrowser``) and ``make ext-edge`` ships that
build. Two daemon paths address the browser by the literal ``"chrome"``:

* ``BrowserAdapter.get_context`` calls ``request_context("chrome")`` and
  ``WebSocketServer.request_context`` matches ``client_type`` exactly, so an
  Edge-only user never contributes tabs, the active tab or the focus goal
  to the task context that every tab-based suggestion is built from.
* ``CortexDaemon._send_leetcode_ws_message`` targets ``["chrome"]``, so the
  LeetCode coach cues never reach Edge (the auto-focus path at the same
  file already targets ``["chrome", "edge"]``).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cortex.services.api_gateway.websocket_server import (
    WebSocketClient,
    WebSocketServer,
    WSMessage,
)
from cortex.services.context_engine.browser_adapter import BrowserAdapter


def test_extension_identifies_as_edge_on_edge() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "apps"
        / "browser_extension"
        / "lib"
        / "browser.ts"
    ).read_text(encoding="utf-8")
    assert 'return "edge";' in source


class _RespondingSocket:
    """Answers each CONTEXT_REQUEST the way background.ts does."""

    def __init__(self, server: WebSocketServer) -> None:
        self._server = server

    async def send(self, raw: str) -> None:
        frame = json.loads(raw)
        if frame.get("type") != "CONTEXT_REQUEST":
            return
        reply = WSMessage(
            type="CONTEXT_RESPONSE",
            payload={
                "browser_context": {
                    "active_tab_title": "Docs",
                    "active_tab_url": "https://docs.python.org/3/",
                    "all_tabs": [],
                },
            },
            correlation_id=frame.get("correlation_id"),
        )
        asyncio.get_running_loop().call_soon(
            self._server._handle_context_response, reply
        )

    async def close(self, code: int = 1000, reason: str = "") -> None:
        return None


@pytest.mark.asyncio
async def test_browser_context_is_collected_from_an_edge_only_user() -> None:
    server = WebSocketServer()
    edge = WebSocketClient(
        client_id="edge-1",
        websocket=_RespondingSocket(server),
        client_type="edge",
    )
    edge.authenticated = True
    server._clients[edge.client_id] = edge

    adapter = BrowserAdapter(request_context_fn=server.request_context)
    context = await adapter.get_context(timeout=1.0)

    assert context is not None
    assert context.active_tab_url == "https://docs.python.org/3/"


@pytest.mark.asyncio
async def test_leetcode_cues_reach_edge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    monkeypatch.setenv("CORTEX_STORAGE__PATH", str(storage))
    from cortex.libs.config import settings as settings_mod

    settings_mod.get_config.cache_clear()
    try:
        from cortex.services.runtime_daemon import CortexDaemon

        daemon = CortexDaemon(config=settings_mod.get_config())
        sent: list[dict[str, Any]] = []

        async def _send_message(message_type: str, payload: dict[str, Any], **kwargs: Any) -> int:
            sent.append({"type": message_type, **kwargs})
            return 1

        daemon._ws_server.send_message = AsyncMock(side_effect=_send_message)  # type: ignore[method-assign]
        await daemon._send_leetcode_ws_message(
            {"type": "LEETCODE_SHOW_SCRATCHPAD", "payload": {"prompts": []}}
        )
    finally:
        settings_mod.get_config.cache_clear()

    assert sent, "the cue was not sent at all"
    assert "edge" in (sent[-1].get("target_client_types") or [])
