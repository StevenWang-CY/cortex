"""Transport-neutral desktop message decoding and replay protection."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

logger = logging.getLogger(__name__)

DesktopMessageHandler = Callable[[dict[str, Any]], None]


class DesktopMessageRouter:
    """Normalize one daemon envelope and route it to a desktop handler."""

    def __init__(self, handlers: Mapping[str, DesktopMessageHandler]) -> None:
        self._handlers = dict(handlers)
        self._last_sequence_by_type: dict[str, int] = {}
        self._current_correlation_id: str | None = None

    def reset(self) -> None:
        self._last_sequence_by_type.clear()

    @property
    def sequence_state(self) -> dict[str, int]:
        """Mutable compatibility view for the legacy bridge test seam."""

        return self._last_sequence_by_type

    @property
    def current_correlation_id(self) -> str | None:
        """The envelope's ``correlation_id`` while its handler runs.

        A handler that must echo the daemon's cid later (USER_ACTION is
        dropped unless it carries the trigger's cid) reads it here; it is
        ``None`` outside a dispatch and for envelopes without one.
        """

        return self._current_correlation_id

    def dispatch_json(self, raw: str) -> bool:
        try:
            envelope = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return False
        if not isinstance(envelope, dict):
            return False
        return self.dispatch(
            str(envelope.get("type", "")),
            envelope.get("payload"),
            sequence=envelope.get("sequence", 0),
            target_client_types=envelope.get("target_client_types"),
            correlation_id=envelope.get("correlation_id"),
        )

    def dispatch(
        self,
        message_type: str,
        payload: object,
        *,
        sequence: object = 0,
        target_client_types: object = None,
        correlation_id: object = None,
    ) -> bool:
        if not message_type:
            return False
        if self._excludes_desktop(target_client_types):
            return False
        if isinstance(sequence, int) and not isinstance(sequence, bool) and sequence > 0:
            previous = self._last_sequence_by_type.get(message_type, 0)
            if sequence <= previous:
                logger.debug(
                    "Dropping stale desktop frame type=%s sequence=%d previous=%d",
                    message_type,
                    sequence,
                    previous,
                )
                return False
            self._last_sequence_by_type[message_type] = sequence
        handler = self._handlers.get(message_type)
        if handler is None:
            return False
        self._current_correlation_id = (
            correlation_id if isinstance(correlation_id, str) and correlation_id else None
        )
        try:
            handler(dict(payload) if isinstance(payload, Mapping) else {})
        finally:
            self._current_correlation_id = None
        return True

    @staticmethod
    def _excludes_desktop(targets: object) -> bool:
        if not isinstance(targets, Sequence) or isinstance(targets, (str, bytes)):
            return False
        normalized = {str(target).lower() for target in targets}
        return bool(normalized) and "desktop" not in normalized
