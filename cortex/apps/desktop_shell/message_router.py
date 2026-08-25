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

    def reset(self) -> None:
        self._last_sequence_by_type.clear()

    @property
    def sequence_state(self) -> dict[str, int]:
        """Mutable compatibility view for the legacy bridge test seam."""

        return self._last_sequence_by_type

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
        )

    def dispatch(
        self,
        message_type: str,
        payload: object,
        *,
        sequence: object = 0,
        target_client_types: object = None,
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
        handler(dict(payload) if isinstance(payload, Mapping) else {})
        return True

    @staticmethod
    def _excludes_desktop(targets: object) -> bool:
        if not isinstance(targets, Sequence) or isinstance(targets, (str, bytes)):
            return False
        normalized = {str(target).lower() for target in targets}
        return bool(normalized) and "desktop" not in normalized
