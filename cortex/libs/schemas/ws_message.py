"""
WebSocket Message Envelope — Pydantic Source of Truth

A single ``WSMessage`` Pydantic model that mirrors the legacy dataclass
in ``cortex/services/api_gateway/websocket_server.py`` and is the source
of truth for the TypeScript ``WSMessage`` interface emitted by the
codegen pipeline.

This closes the structural half of Debt-1 for the WS envelope: the
extension's hand-written interface (``background.ts:23``) gets replaced
with the generated type in Commit 4.

Backwards compatibility
-----------------------

The legacy dataclass stays in place for one release. New code is
expected to construct ``WSMessage`` through this Pydantic model; the
dataclass's ``to_json`` / ``from_json`` are rewritten in
``websocket_server.py`` to round-trip through the Pydantic version so
the wire format is bit-for-bit identical.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from cortex.libs.schemas.ws_message_types import MessageType


class WSMessage(BaseModel):
    """A WebSocket message exchanged between the Cortex daemon and clients.

    Field set is identical to the legacy dataclass; the only change is
    that ``type`` is a ``MessageType`` enum so callers cannot send a
    typo at the wire boundary (F45 closure).

    Serialisation contract
    ----------------------

    ``model_dump_json()`` produces JSON with ``"type": "STATE_UPDATE"``
    style string values (not the enum's ``str(member)`` repr). This is
    guaranteed by ``ConfigDict(use_enum_values=True)`` so the wire
    format matches the legacy dataclass's ``json.dumps`` output.

    Field ordering matches the legacy dataclass for stability of the
    generated TypeScript interface and the recorded session JSONL.
    """

    model_config = ConfigDict(
        use_enum_values=True,
        # Stay liberal on the input side — unknown keys are ignored so
        # a future schema bump doesn't crash older clients mid-frame.
        extra="ignore",
        # Pydantic v2 default is to validate on construction; we keep
        # that so a typo at the call site (e.g. ``WSMessage(type="STAT_UPDATE")``)
        # fails fast in tests.
        validate_assignment=True,
    )

    type: MessageType = Field(
        ..., description="Wire-level message type; see ``MessageType``."
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Message-specific JSON-serialisable payload.",
    )
    timestamp: float = Field(
        default_factory=time.monotonic,
        description="Monotonic timestamp at construction time.",
    )
    sequence: int = Field(
        default=0,
        description=(
            "Monotonically-increasing sequence number assigned by the "
            "producer; clients drop frames with stale sequences (F17)."
        ),
    )
    correlation_id: str | None = Field(
        default=None,
        description=(
            "End-to-end correlation id (F19). Threaded from the original "
            "user action through every layer that touches this message."
        ),
    )
    target_client_types: list[str] | None = Field(
        default=None,
        description=(
            "If set, only clients whose ``client_type`` appears in this "
            "list receive the message. None = broadcast to all."
        ),
    )
    source_client_type: str | None = Field(
        default=None,
        description=(
            "Producer's identity (``daemon``, ``chrome``, ``desktop``, "
            "``vscode``). Receivers can route on this without parsing the "
            "payload."
        ),
    )

    def to_json(self) -> str:
        """Serialise to a JSON string matching the legacy wire format."""
        return self.model_dump_json()

    @classmethod
    def from_json(cls, data: str) -> WSMessage:
        """Parse a wire-format JSON string into a ``WSMessage``."""
        return cls.model_validate_json(data)
