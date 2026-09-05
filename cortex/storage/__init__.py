"""Authoritative local persistence for Cortex.

The storage package deliberately owns no domain decisions.  It provides one
rollback-journal SQLite connection, typed repositories, bounded best-effort
analytics ingestion, and explicit maintenance operations.  Domain services
continue to validate their own Pydantic aggregates before crossing this
boundary.
"""

from cortex.storage.database import (
    CORTEX_APPLICATION_ID,
    CURRENT_SCHEMA_VERSION,
    DEFAULT_SESSION_RETENTION_DAYS,
    MINIMUM_SQLITE_VERSION,
    SQLiteDatabase,
    StorageBusyError,
    StorageCapacityError,
    StorageCompatibilityError,
    StorageCorruptionError,
    StorageError,
    StorageReadOnlyError,
)

__all__ = [
    "CORTEX_APPLICATION_ID",
    "CURRENT_SCHEMA_VERSION",
    "DEFAULT_SESSION_RETENTION_DAYS",
    "MINIMUM_SQLITE_VERSION",
    "SQLiteDatabase",
    "StorageBusyError",
    "StorageCapacityError",
    "StorageCompatibilityError",
    "StorageCorruptionError",
    "StorageError",
    "StorageReadOnlyError",
]
