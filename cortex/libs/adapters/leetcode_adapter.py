"""
LeetCode Adapter

Bridges the Cortex runtime to a LeetCode browser extension via WebSocket.
Sends intervention commands (lock editor, show scratchpad, gate solutions, etc.)
and caches DOM-derived context updates from the extension.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Coroutine

from cortex.libs.adapters.base import AdapterResult, CortexAdapter
from cortex.libs.schemas.leetcode import LeetCodeContext

logger = logging.getLogger(__name__)

# Type alias for the async WebSocket sender callback
WsSender = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]


class LeetCodeAdapter:
    """
    Adapter that communicates with a LeetCode browser extension over WebSocket.

    Outbound: sends ``{"type": "LEETCODE_<ACTION>", "payload": ...}`` commands.
    Inbound:  caches ``LeetCodeContext`` snapshots pushed by the extension.
    """

    _CAPABILITIES: list[str] = [
        "lock_editor",
        "intercept_submit",
        "gate_solutions",
        "show_scratchpad",
        "show_pattern_ladder",
        "show_lockout",
        "show_consolidation",
        "show_submission_gate",
        "show_solution_friction",
        "show_session_briefing",
        "ai_restatement_check",
        "ai_comprehension_check",
        "ai_hypothesis_check",
        "ai_stuck_analysis",
        "ai_session_briefing",
    ]

    def __init__(self) -> None:
        self._send_ws_message: WsSender | None = None
        self._context: LeetCodeContext = LeetCodeContext()
        self._last_context_update: float = 0.0

    # -- CortexAdapter protocol -----------------------------------------------

    @property
    def name(self) -> str:
        return "leetcode"

    @property
    def capabilities(self) -> list[str]:
        return list(self._CAPABILITIES)

    async def execute(self, action: str, params: dict[str, Any]) -> AdapterResult:
        """Send an intervention command to the browser extension."""
        if self._send_ws_message is None:
            return AdapterResult(
                success=False,
                error="WebSocket sender not configured — call set_ws_sender() first",
            )

        if action not in self._CAPABILITIES:
            return AdapterResult(
                success=False,
                error=f"Unknown action '{action}'. Valid: {self._CAPABILITIES}",
            )

        message = {
            "type": f"LEETCODE_{action.upper()}",
            "payload": params,
        }

        try:
            await self._send_ws_message(message)
        except Exception as exc:
            logger.exception("Failed to send WS message for action '%s'", action)
            return AdapterResult(success=False, error=str(exc))

        return AdapterResult(success=True, data={"action": action, "params": params})

    async def get_context(self) -> dict[str, Any]:
        """Return the latest cached LeetCode context as a dict."""
        return self._context.model_dump()

    async def health_check(self) -> bool:
        """Healthy when a WS sender is set and context was updated within 5 s."""
        if self._send_ws_message is None:
            return False
        return (time.time() - self._last_context_update) <= 5.0

    # -- Extension-facing helpers ---------------------------------------------

    def set_ws_sender(self, sender: WsSender) -> None:
        """Register the async callback used to push messages over WebSocket."""
        self._send_ws_message = sender
        logger.info("LeetCode adapter: WebSocket sender registered")

    def update_context(self, data: dict[str, Any]) -> None:
        """
        Update the cached context from an incoming ``LEETCODE_CONTEXT`` message.

        Parameters
        ----------
        data:
            Raw dict matching the ``LeetCodeContext`` schema, typically sent by
            the DOM observer content script at ~1 Hz.
        """
        self._context = LeetCodeContext.model_validate(data)
        self._last_context_update = time.time()

    @property
    def context(self) -> LeetCodeContext:
        """Direct access to the cached ``LeetCodeContext`` model."""
        return self._context
