"""
Throttle — Copilot/Cursor Inline Suggestion Throttle

Connects the state engine to the VS Code API to silence inline AI
suggestions (Copilot, Cursor, etc.) when the user is overwhelmed.

When HYPER is detected with high confidence, sends a command to the
VS Code extension to disable inline suggestions. Re-enables on FLOW.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class CopilotThrottle:
    """
    Manages AI assistant throttling based on cognitive state.

    Sends commands to the VS Code extension via WebSocket to toggle
    inline suggestions. Tracks current throttle state to avoid
    sending redundant commands.

    Usage:
        throttle = CopilotThrottle(ws_server=ws_server)
        await throttle.on_state_change("HYPER", confidence=0.9)
    """

    def __init__(
        self,
        ws_server: Any = None,
        hyper_threshold: float = 0.85,
        flow_threshold: float = 0.70,
    ) -> None:
        self._ws_server = ws_server
        self._hyper_threshold = hyper_threshold
        self._flow_threshold = flow_threshold
        self._is_throttled = False
        self._enabled = True

    @property
    def is_throttled(self) -> bool:
        """Whether inline suggestions are currently throttled."""
        return self._is_throttled

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value
        if not value and self._is_throttled:
            # Re-enable when feature is disabled
            self._is_throttled = False

    async def on_state_change(
        self,
        state: str,
        confidence: float,
    ) -> bool:
        """
        React to a cognitive state change.

        Throttles on HYPER with high confidence, un-throttles on FLOW.

        Args:
            state: Current state ("FLOW", "HYPER", "HYPO", "RECOVERY").
            confidence: State confidence (0-1).

        Returns:
            True if throttle state changed.
        """
        if not self._enabled:
            return False

        if state == "HYPER" and confidence >= self._hyper_threshold and not self._is_throttled:
            await self._disable_suggestions()
            self._is_throttled = True
            logger.info(
                "Copilot throttled: HYPER at %.0f%% confidence",
                confidence * 100,
            )
            return True

        if state == "FLOW" and confidence >= self._flow_threshold and self._is_throttled:
            await self._enable_suggestions()
            self._is_throttled = False
            logger.info(
                "Copilot un-throttled: FLOW at %.0f%% confidence",
                confidence * 100,
            )
            return True

        return False

    async def _disable_suggestions(self) -> None:
        """Send command to VS Code to disable inline suggestions."""
        if self._ws_server is None:
            return
        try:
            await self._ws_server.send_to_client(
                client_type="vscode",
                message_type="COMMAND",
                payload={
                    "command": "cortex.disableInlineSuggestions",
                    "args": {},
                },
            )
        except Exception:
            logger.debug("Failed to send disable suggestions command")

    async def _enable_suggestions(self) -> None:
        """Send command to VS Code to re-enable inline suggestions."""
        if self._ws_server is None:
            return
        try:
            await self._ws_server.send_to_client(
                client_type="vscode",
                message_type="COMMAND",
                payload={
                    "command": "cortex.enableInlineSuggestions",
                    "args": {},
                },
            )
        except Exception:
            logger.debug("Failed to send enable suggestions command")

    async def force_enable(self) -> None:
        """Force re-enable suggestions regardless of state."""
        if self._is_throttled:
            await self._enable_suggestions()
            self._is_throttled = False
            logger.info("Copilot force-enabled")
