"""Contained legacy Copilot/Cursor inline-suggestion policy.

State estimates may inform a proposal, but they do not grant authority to
change editor settings. The compatibility state machine remains decodable for
old configuration and cleanup paths; its forward wire command is intentionally
inert until represented by an exact WP6 manifest/authorization/receipt chain.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class CopilotThrottle:
    """
    Manages AI assistant throttling based on cognitive state.

    Evaluates the old policy without emitting editor mutation commands.

    ``on_state_change`` returns ``False`` unless a future exact adapter is
    wired; callers therefore never claim a settings change from policy alone.
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

        Evaluate the legacy thresholds without granting workspace authority.

        Args:
            state: Current state ("FLOW", "HYPER", "HYPO", "RECOVERY").
            confidence: State confidence (0-1).

        Returns:
            True if throttle state changed.
        """
        if not self._enabled:
            return False

        if state == "HYPER" and confidence >= self._hyper_threshold and not self._is_throttled:
            if await self._disable_suggestions():
                self._is_throttled = True
                logger.info(
                    "Copilot throttled: HYPER at %.0f%% confidence",
                    confidence * 100,
                )
                return True
            return False

        # P1: re-enable on ANY confirmed transition OUT of HYPER, not just
        # a clean FLOW recovery. The previous code only un-throttled on
        # ``state == "FLOW"`` with sufficient confidence; on the common
        # FLOW→HYPER→HYPO and HYPER→RECOVERY paths the user never returns
        # straight to FLOW, so suggestions stayed silenced forever. The
        # state machine only enters the throttle from HYPER, so leaving
        # HYPER (to FLOW / HYPO / RECOVERY) is the correct release edge.
        # FLOW still carries the confidence gate (a low-confidence FLOW
        # blip shouldn't toggle), while HYPO / RECOVERY release
        # unconditionally — leaving HYPER at all means the overwhelm that
        # justified throttling is over.
        if self._is_throttled and state != "HYPER":
            if state == "FLOW" and confidence < self._flow_threshold:
                return False
            if await self._enable_suggestions():
                self._is_throttled = False
                logger.info(
                    "Copilot un-throttled: left HYPER for %s at %.0f%% confidence",
                    state,
                    confidence * 100,
                )
                return True
            return False

        return False

    async def _disable_suggestions(self) -> bool:
        """Tell VS Code to disable inline suggestions (Copilot/Cursor/…)."""
        return await self._emit("disable")

    async def _enable_suggestions(self) -> bool:
        """Tell VS Code to re-enable inline suggestions."""
        return await self._emit("enable")

    async def _emit(self, action: str) -> bool:
        """Contain the legacy mutation until it uses exact authorization."""

        logger.info(
            "COPILOT_THROTTLE %s suppressed pending exact transaction support",
            action,
        )
        return False

    async def force_enable(self) -> None:
        """Force re-enable suggestions regardless of state."""
        if self._is_throttled:
            if await self._enable_suggestions():
                self._is_throttled = False
                logger.info("Copilot force-enabled")
