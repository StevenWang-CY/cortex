"""Unambiguous time metadata shared by versioned external contracts."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


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
