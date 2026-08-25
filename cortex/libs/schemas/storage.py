"""Wire and durable-event contracts for local storage operations."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cortex.application.clock import Clock
from cortex.libs.schemas.temporal import DualClockModel

StorageExportCategory = Literal[
    "consent",
    "interventions",
    "policy",
    "calibration",
    "sessions",
    "derived",
]
StorageDeleteScope = Literal[
    "consent",
    "interventions",
    "policy",
    "calibration",
    "sessions",
    "derived",
    "analytics",
    "all",
]


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


class StoredAnalyticsEvent(BaseModel):
    """Bounded, content-addressed derived event accepted by the writer.

    Callers supply a canonical JSON string rather than an open dictionary so
    the exact persisted payload cannot change after queue admission.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    event_id: UUID = Field(default_factory=uuid4)
    event_type: str = Field(..., min_length=1, max_length=96)
    aggregate_type: str = Field(..., min_length=1, max_length=64)
    aggregate_id: str | None = Field(None, max_length=128)
    occurred_at_unix_ms: int = Field(..., ge=0)
    occurred_at_mono_ns: int = Field(..., ge=0)
    boot_id: UUID
    privacy_class: Literal["operational", "derived", "sensitive_derived"]
    payload_json: str = Field(..., min_length=2, max_length=65_536)
    payload_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    expires_at_unix_ms: int = Field(..., ge=0)

    @field_validator("payload_json")
    @classmethod
    def _payload_is_canonical_object(cls, value: str) -> str:
        decoded = json.loads(value)
        if not isinstance(decoded, dict) or _canonical_json(decoded) != value:
            raise ValueError("payload_json must be a canonical JSON object")
        return value

    @model_validator(mode="after")
    def _validate_digest_and_expiry(self) -> StoredAnalyticsEvent:
        digest = hashlib.sha256(self.payload_json.encode("utf-8")).hexdigest()
        if digest != self.payload_sha256:
            raise ValueError("payload_sha256 does not match payload_json")
        if self.expires_at_unix_ms < self.occurred_at_unix_ms:
            raise ValueError("analytics event expires before it occurs")
        return self

    @classmethod
    def create(
        cls,
        clock: Clock,
        *,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str | None,
        privacy_class: Literal["operational", "derived", "sensitive_derived"],
        payload: dict[str, Any],
        retention_seconds: int,
    ) -> StoredAnalyticsEvent:
        if retention_seconds < 1:
            raise ValueError("retention_seconds must be positive")
        encoded = _canonical_json(payload)
        now = clock.unix_ms()
        return cls(
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            occurred_at_unix_ms=now,
            occurred_at_mono_ns=clock.monotonic_ns(),
            boot_id=clock.boot_id,
            privacy_class=privacy_class,
            payload_json=encoded,
            payload_sha256=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            expires_at_unix_ms=now + retention_seconds * 1_000,
        )


class StorageHealthReport(BaseModel):
    """Path-redacted local database health exposed over loopback APIs."""

    model_config = ConfigDict(extra="forbid")

    healthy: bool
    degraded: bool
    backend: Literal["sqlite"] = "sqlite"
    journal_mode: Literal["delete", "unavailable"]
    synchronous: Literal["full", "unsupported", "unavailable"]
    foreign_keys: bool
    schema_version: int = Field(..., ge=0)
    sqlite_version: str
    database_filename: str
    database_bytes: int = Field(..., ge=0)
    pending_operations: int = Field(..., ge=0)
    analytics_queue_depth: int = Field(0, ge=0)
    analytics_dropped_total: int = Field(0, ge=0)
    last_integrity_check_unix_ms: int | None = Field(None, ge=0)
    error_code: str | None = Field(None, max_length=96)
    record_counts: dict[str, int] = Field(default_factory=dict)


class StorageStatusResponse(DualClockModel):
    storage: StorageHealthReport
    retention_days: dict[str, int]
    exports_directory_name: str = "exports"


class StorageExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    categories: tuple[StorageExportCategory, ...] = (
        "consent",
        "interventions",
        "policy",
        "calibration",
        "sessions",
        "derived",
    )

    @field_validator("categories")
    @classmethod
    def _categories_are_unique(
        cls,
        value: tuple[StorageExportCategory, ...],
    ) -> tuple[StorageExportCategory, ...]:
        if not value:
            raise ValueError("at least one export category is required")
        if len(value) != len(set(value)):
            raise ValueError("export categories must be unique")
        return value


class StorageExportResponse(DualClockModel):
    export_id: UUID
    filename: str
    sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    bytes_written: int = Field(..., ge=0)
    record_counts: dict[str, int]


class StorageDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scopes: tuple[StorageDeleteScope, ...]
    confirmation: Literal["DELETE CORTEX DATA"]

    @field_validator("scopes")
    @classmethod
    def _scopes_are_canonical(
        cls,
        value: tuple[StorageDeleteScope, ...],
    ) -> tuple[StorageDeleteScope, ...]:
        if not value:
            raise ValueError("at least one deletion scope is required")
        if len(value) != len(set(value)):
            raise ValueError("deletion scopes must be unique")
        if "all" in value and len(value) != 1:
            raise ValueError("'all' cannot be combined with another scope")
        return value


class StorageDeleteResponse(DualClockModel):
    deleted_counts: dict[str, int]
    vacuumed: bool


__all__ = [
    "StorageDeleteRequest",
    "StorageDeleteResponse",
    "StorageExportCategory",
    "StorageExportRequest",
    "StorageExportResponse",
    "StorageHealthReport",
    "StorageStatusResponse",
    "StoredAnalyticsEvent",
]
