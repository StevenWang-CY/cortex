"""
Context Engine — Editor Adapter

Communicates with the VS Code extension via WebSocket to gather
EditorContext (file path, visible range, diagnostics, cursor symbol,
visible code).

When the extension is unavailable, falls back gracefully with None.

Usage:
    adapter = EditorAdapter()
    ctx = await adapter.get_context()
    if ctx is not None:
        print(ctx.file_path, ctx.error_count)
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from cortex.libs.schemas.context import Diagnostic, EditorContext

logger = logging.getLogger(__name__)


class EditorAdapter:
    """
    Adapter for gathering VS Code editor context.

    Connects to the VS Code extension via an internal WebSocket channel
    and requests current editor state. Falls back to None when the
    extension is unavailable.

    Usage:
        adapter = EditorAdapter(ws_send_fn=send, ws_receive_fn=receive)
        ctx = await adapter.get_context(timeout=2.0)
    """

    def __init__(
        self,
        ws_send_fn: Any | None = None,
        ws_receive_fn: Any | None = None,
    ) -> None:
        """
        Args:
            ws_send_fn: Async callable to send a message to VS Code extension.
            ws_receive_fn: Async callable to receive a response from extension.
        """
        self._ws_send = ws_send_fn
        self._ws_receive = ws_receive_fn
        self._available = False
        self._last_context: EditorContext | None = None

    @property
    def available(self) -> bool:
        return self._available

    @property
    def last_context(self) -> EditorContext | None:
        return self._last_context

    async def get_context(self, timeout: float = 2.0) -> EditorContext | None:
        """
        Request editor context from VS Code extension.

        Args:
            timeout: Maximum seconds to wait for response.

        Returns:
            EditorContext if extension responds, None otherwise.
        """
        if self._ws_send is None or self._ws_receive is None:
            self._available = False
            return None

        try:
            # Send context request to extension
            request = json.dumps({
                "type": "CONTEXT_REQUEST",
                "payload": {"commands": [
                    "cortex.getActiveFile",
                    "cortex.getDiagnostics",
                    "cortex.getSymbolAtCursor",
                ]},
            })

            await asyncio.wait_for(self._ws_send(request), timeout=timeout)

            # Wait for response
            raw = await asyncio.wait_for(self._ws_receive(), timeout=timeout)
            data = json.loads(raw)

            if data.get("type") != "CONTEXT_RESPONSE":
                logger.debug(f"Unexpected response type: {data.get('type')}")
                self._available = False
                return None

            ctx = self._parse_editor_context(data.get("payload", {}))
            self._available = True
            self._last_context = ctx
            return ctx

        except (asyncio.TimeoutError, ConnectionError, OSError) as e:
            logger.debug(f"Editor adapter unavailable: {e}")
            self._available = False
            return None
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning(f"Invalid editor context response: {e}")
            self._available = False
            return None

    def update_from_payload(self, payload: dict) -> EditorContext | None:
        """
        Update context directly from a payload dict (for push-based updates).

        Args:
            payload: Editor context payload dict.

        Returns:
            Parsed EditorContext, or None on parse error.
        """
        try:
            ctx = self._parse_editor_context(payload)
            self._available = True
            self._last_context = ctx
            return ctx
        except (KeyError, TypeError, ValueError) as e:
            logger.warning(f"Failed to parse editor context: {e}")
            return None

    @staticmethod
    def _parse_editor_context(payload: dict) -> EditorContext:
        """Parse an EditorContext from a payload dict."""
        diagnostics = []
        for d in payload.get("diagnostics", []):
            diagnostics.append(Diagnostic(
                severity=d.get("severity", "info"),
                message=d.get("message", ""),
                line=d.get("line", 1),
                column=d.get("column", 0),
                source=d.get("source"),
                code=d.get("code"),
            ))

        return EditorContext(
            file_path=payload.get("file_path", ""),
            visible_range=tuple(payload.get("visible_range", (1, 50))),
            symbol_at_cursor=payload.get("symbol_at_cursor"),
            diagnostics=diagnostics,
            recent_edits=payload.get("recent_edits", []),
            visible_code=payload.get("visible_code", ""),
        )

    def reset(self) -> None:
        """Reset adapter state."""
        self._available = False
        self._last_context = None
