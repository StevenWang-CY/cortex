"""Unambiguous time metadata shared by versioned external contracts."""

from __future__ import annotations

from typing import Any, Literal, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cortex.application.clock import SYSTEM_CLOCK, Clock

WIRE_SCHEMA_VERSION = "2.0"
LEGACY_BOOT_ID = UUID(int=0)


class EventTime(BaseModel):
    """Dual-clock metadata for one observation or domain event."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["2.0"] = "2.0"
    observed_at_unix_ms: int = Field(
        ...,
        ge=0,
        description="UTC Unix epoch milliseconds for display and persistence",
    )
    observed_at_mono_ns: int = Field(
        ...,
        ge=0,
        description="Process-local monotonic nanoseconds for elapsed-time ordering",
    )
    boot_id: UUID = Field(
        ...,
        description="Monotonic clock domain; values compare only within one boot_id",
    )

    @classmethod
    def from_clock(cls, clock: Clock) -> EventTime:
        """Capture both clocks at one explicit event boundary."""

        return cls(
            observed_at_unix_ms=clock.unix_ms(),
            observed_at_mono_ns=clock.monotonic_ns(),
            boot_id=clock.boot_id,
        )


class EventMetadata(BaseModel):
    """Identity, ordering, causality, and dual-clock metadata for a wire event."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["2.0"] = "2.0"
    event_id: UUID = Field(default_factory=uuid4)
    sequence: int = Field(0, ge=0)
    observed_at_unix_ms: int = Field(..., ge=0)
    observed_at_mono_ns: int = Field(..., ge=0)
    boot_id: UUID
    correlation_id: UUID | str | None = None
    causation_id: UUID | str | None = None

    @classmethod
    def from_clock(
        cls,
        clock: Clock,
        *,
        sequence: int = 0,
        correlation_id: UUID | str | None = None,
        causation_id: UUID | str | None = None,
        event_id: UUID | None = None,
    ) -> EventMetadata:
        """Construct deterministic metadata using an injected clock."""

        return cls(
            event_id=event_id or uuid4(),
            sequence=sequence,
            observed_at_unix_ms=clock.unix_ms(),
            observed_at_mono_ns=clock.monotonic_ns(),
            boot_id=clock.boot_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )


class PersistedDeadline(BaseModel):
    """Wire/storage representation of a reboot-safe bounded deadline."""

    model_config = ConfigDict(extra="forbid")

    expires_at_unix_ms: int = Field(..., ge=0)
    duration_ms: int = Field(..., ge=0)
    created_at_unix_ms: int = Field(..., ge=0)
    created_at_mono_ns: int = Field(..., ge=0)
    boot_id: UUID


class DualClockModel(BaseModel):
    """Base for external payloads during the v1 → v2 time migration.

    New construction emits explicit v2 dual-clock fields and a deprecated
    epoch-seconds mirror. Decoding an explicit legacy ``timestamp`` never
    invents monotonic provenance: the monotonic value and boot ID are the
    documented legacy sentinels.
    """

    model_config = ConfigDict(extra="ignore")

    schema_version: Literal["1.0", "2.0"] = "2.0"
    # Sentinels make inherited Pydantic constructor signatures optional for
    # static checking. ``_populate_time`` replaces them before validation.
    observed_at_unix_ms: int = Field(0, ge=0)
    observed_at_mono_ns: int = Field(0, ge=0)
    boot_id: UUID = LEGACY_BOOT_ID
    timestamp: float | None = Field(
        None,
        deprecated=True,
        description=(
            "Deprecated v1 compatibility mirror in UTC Unix seconds; "
            "never use it for elapsed-time decisions."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _populate_time(cls, raw: object) -> object:
        if not isinstance(raw, dict):
            return raw
        values = dict(raw)
        time_keys = ("observed_at_unix_ms", "observed_at_mono_ns", "boot_id")
        supplied = tuple(key in values for key in time_keys)
        if any(supplied) and not all(supplied):
            raise ValueError(
                "observed_at_unix_ms, observed_at_mono_ns, and boot_id "
                "must be supplied together"
            )
        complete = all(supplied)
        legacy = values.get("timestamp")
        if not complete and legacy is not None:
            values.setdefault("schema_version", "1.0")
            values["observed_at_unix_ms"] = max(0, int(float(legacy) * 1000))
            values["observed_at_mono_ns"] = 0
            values["boot_id"] = LEGACY_BOOT_ID
        elif not complete:
            values["observed_at_unix_ms"] = SYSTEM_CLOCK.unix_ms()
            values["observed_at_mono_ns"] = SYSTEM_CLOCK.monotonic_ns()
            values["boot_id"] = SYSTEM_CLOCK.boot_id
        if values.get("timestamp") is None:
            values["timestamp"] = int(values["observed_at_unix_ms"]) / 1000.0
        return values

    @classmethod
    def time_fields(cls, clock: Clock) -> dict[str, object]:
        """Return one coherent v2 time tuple for explicit construction."""

        unix_ms = clock.unix_ms()
        return {
            "schema_version": "2.0",
            "observed_at_unix_ms": unix_ms,
            "observed_at_mono_ns": clock.monotonic_ns(),
            "boot_id": clock.boot_id,
            "timestamp": unix_ms / 1000.0,
        }

    @classmethod
    def from_clock(cls, clock: Clock, **values: Any) -> Self:
        """Construct a response with one coherent, explicitly supplied tuple."""

        return cls.model_validate({**cls.time_fields(clock), **values})
