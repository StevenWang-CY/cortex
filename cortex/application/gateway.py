"""Typed command boundary between transport adapters and the application."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, fields
from typing import Any

HandlerResult = Any | Awaitable[Any]
CommandHandler = Callable[..., HandlerResult]


@dataclass(frozen=True, slots=True)
class WebSocketCommandHandlers:
    """One immutable binding for every command accepted by WebSocket.

    Optional handlers let the transport run in isolation in contract tests.
    Production binds the complete bundle once at the composition root.
    """

    user_action: CommandHandler | None = None
    settings: CommandHandler | None = None
    calibration_reload: CommandHandler | None = None
    shutdown: CommandHandler | None = None
    activity_sync: CommandHandler | None = None
    tab_relevance_feedback: CommandHandler | None = None
    leetcode_context: CommandHandler | None = None
    intervention_applied: CommandHandler | None = None
    intervention_authorize: CommandHandler | None = None
    intervention_receipt: CommandHandler | None = None
    intervention_dispatch_failure: CommandHandler | None = None
    intervention_partial_dispatch: CommandHandler | None = None
    intervention_dispatch_binding: CommandHandler | None = None
    client_identified: CommandHandler | None = None
    session_list: CommandHandler | None = None
    session_detail: CommandHandler | None = None
    trends: CommandHandler | None = None
    micro_step_toggled: CommandHandler | None = None
    why_detail: CommandHandler | None = None
    session_recap_cache: CommandHandler | None = None
    session_recap_acknowledged: CommandHandler | None = None
    quiet_mode_toggle: CommandHandler | None = None

    def as_callback_attributes(self) -> dict[str, CommandHandler | None]:
        return {
            f"_{field.name}_callback": getattr(self, field.name)
            for field in fields(self)
        }
