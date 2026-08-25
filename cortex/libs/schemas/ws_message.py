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

from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cortex.application.clock import SYSTEM_CLOCK, Clock
from cortex.libs.schemas.temporal import LEGACY_BOOT_ID
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
    schema_version: str = Field(
        "2.0",
        pattern=r"^\d+\.\d+$",
        description="Envelope schema version. Legacy decoded frames are 1.0.",
    )
    protocol_version: str = Field(
        "2.0",
        pattern=r"^\d+\.\d+$",
        description="Negotiated transport protocol version.",
    )
    event_id: UUID = Field(
        default_factory=uuid4,
        description="Globally unique event identity used for idempotency.",
    )
    sent_at_unix_ms: int = Field(
        default_factory=SYSTEM_CLOCK.unix_ms,
        ge=0,
        description="UTC Unix epoch milliseconds for persistence and display.",
    )
    sent_at_mono_ns: int = Field(
        default_factory=SYSTEM_CLOCK.monotonic_ns,
        ge=0,
        description="Producer-local monotonic nanoseconds for elapsed ordering.",
    )
    boot_id: UUID = Field(
        default_factory=lambda: SYSTEM_CLOCK.boot_id,
        description="Producer clock domain; monotonic values compare only within it.",
    )
    timestamp: float | None = Field(
        None,
        deprecated=True,
        description=(
            "Deprecated v1 compatibility mirror of sent_at_unix_ms in UTC "
            "Unix seconds. Never use for elapsed-time decisions."
        ),
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
    causation_id: str | None = Field(
        default=None,
        description="Event ID or command ID that directly caused this event.",
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

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_time(cls, raw: Any) -> Any:
        """Dual-read v1 epoch seconds and v2 explicit dual-clock metadata.

        A v1 frame has no monotonic provenance. It receives the all-zero
        legacy boot domain and monotonic value instead of pretending its
        epoch timestamp came from a monotonic clock.
        """

        if not isinstance(raw, dict):
            return raw
        values = dict(raw)
        time_keys = ("sent_at_unix_ms", "sent_at_mono_ns", "boot_id")
        supplied = tuple(key in values for key in time_keys)
        if any(supplied) and not all(supplied):
            raise ValueError(
                "sent_at_unix_ms, sent_at_mono_ns, and boot_id must be supplied together"
            )
        has_v2_time = all(supplied)
        legacy = values.get("timestamp")
        if not has_v2_time and legacy is not None:
            values.setdefault("schema_version", "1.0")
            values.setdefault("protocol_version", "1.0")
            values["sent_at_unix_ms"] = max(0, int(float(legacy) * 1000))
            values["sent_at_mono_ns"] = 0
            values["boot_id"] = LEGACY_BOOT_ID
        elif not has_v2_time:
            values["sent_at_unix_ms"] = SYSTEM_CLOCK.unix_ms()
            values["sent_at_mono_ns"] = SYSTEM_CLOCK.monotonic_ns()
            values["boot_id"] = SYSTEM_CLOCK.boot_id
        if values.get("timestamp") is None:
            values["timestamp"] = int(values["sent_at_unix_ms"]) / 1000.0
        return values

    @classmethod
    def from_clock(
        cls,
        *,
        clock: Clock,
        type: MessageType | str,
        payload: dict[str, Any] | None = None,
        sequence: int = 0,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        target_client_types: list[str] | None = None,
        source_client_type: str | None = None,
        protocol_version: str = "2.0",
        event_id: UUID | None = None,
    ) -> WSMessage:
        """Construct a v2 event with one injected clock capture."""

        unix_ms = clock.unix_ms()
        canonical_type = type if isinstance(type, MessageType) else MessageType(type)
        return cls(
            type=canonical_type,
            payload=payload or {},
            schema_version="2.0",
            protocol_version=protocol_version,
            event_id=event_id or uuid4(),
            sent_at_unix_ms=unix_ms,
            sent_at_mono_ns=clock.monotonic_ns(),
            boot_id=clock.boot_id,
            timestamp=unix_ms / 1000.0,
            sequence=sequence,
            correlation_id=correlation_id,
            causation_id=causation_id,
            target_client_types=target_client_types,
            source_client_type=source_client_type,
        )

    def to_json(self) -> str:
        """Serialise to a JSON string matching the legacy wire format."""
        return self.model_dump_json()

    @classmethod
    def from_json(cls, data: str) -> WSMessage:
        """Parse a wire-format JSON string into a ``WSMessage``."""
        return cls.model_validate_json(data)
