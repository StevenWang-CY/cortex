"""SQLite-backed compatibility store for durable small state."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
from collections import deque
from pathlib import Path
from typing import Any

from cortex.application.clock import SYSTEM_CLOCK, Clock
from cortex.storage.database import SQLiteDatabase, StorageCorruptionError
from cortex.storage.intervention_store import _atomic_copy_bytes

_TIMESERIES_MAXLEN = 10_000
_MIGRATABLE_EXACT_KEYS = {
    "bandit_weights",
    "consent_ladder_state",
    "tab_relevance_domains",
}
_MIGRATABLE_PREFIXES = (
    "daily_baseline:",
    "helpfulness:",
    "modality_pref:",
    "tab_relevance:",
)
logger = logging.getLogger(__name__)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_legacy_key(key: str) -> bool:
    """Whitelist known non-secret legacy records.

    Unknown state is intentionally skipped instead of copying an opaque value
    that might be an API token or raw workspace cache into the authoritative
    database.
    """

    return key in _MIGRATABLE_EXACT_KEYS or key.startswith(_MIGRATABLE_PREFIXES)


class SQLiteKeyValueStore:
    """Implement the existing async key/value API on the shared database.

    Timeseries are intentionally process-local: no production service uses the
    legacy timeseries methods, and persisting a high-rate series through this
    compatibility API would violate the bounded analytics-writer rule.
    """

    def __init__(
        self,
        database: SQLiteDatabase,
        *,
        key_prefix: str = "cortex",
        clock: Clock | None = None,
        legacy_store_path: str | Path | None = None,
        legacy_consent_overrides_path: str | Path | None = None,
    ) -> None:
        self._database = database
        self._namespace = key_prefix
        self._clock = clock or SYSTEM_CLOCK
        self._legacy_store_path = (
            Path(legacy_store_path).expanduser().resolve()
            if legacy_store_path is not None
            else None
        )
        self._legacy_consent_path = (
            Path(legacy_consent_overrides_path).expanduser().resolve()
            if legacy_consent_overrides_path is not None
            else None
        )
        self._legacy_lock = asyncio.Lock()
        self._legacy_checked = False
        self._timeseries: dict[str, deque[tuple[float, float]]] = {}

    @property
    def degraded(self) -> bool:
        return bool(self._database.last_error)

    async def _ready(self) -> None:
        await self._database.start()
        await self._ensure_legacy_migrated()

    async def append_timeseries(self, key: str, timestamp: float, value: float) -> None:
        if not math.isfinite(timestamp) or not math.isfinite(value):
            raise ValueError("timeseries values must be finite")
        series = self._timeseries.setdefault(key, deque(maxlen=_TIMESERIES_MAXLEN))
        series.append((timestamp, value))

    async def get_timeseries(
        self,
        key: str,
        window_seconds: float,
    ) -> list[tuple[float, float]]:
        if window_seconds < 0:
            raise ValueError("window_seconds must be non-negative")
        cutoff = time.time() - window_seconds
        return [item for item in self._timeseries.get(key, ()) if item[0] >= cutoff]

    async def get_json(self, key: str) -> dict[str, Any] | None:
        await self._ready()
        now = self._clock.unix_ms()

        def read_value(connection: Any) -> tuple[str, str, int | None] | None:
            row = connection.execute(
                "SELECT value_json, value_sha256, expires_at_unix_ms "
                "FROM key_values WHERE namespace=? AND key=? AND value_kind='json'",
                (self._namespace, key),
            ).fetchone()
            if row is None:
                return None
            return (
                str(row["value_json"]),
                str(row["value_sha256"]),
                int(row["expires_at_unix_ms"]) if row["expires_at_unix_ms"] is not None else None,
            )

        stored = await self._database.read(read_value)
        if stored is None:
            return None
        encoded, digest, expires_at = stored
        if expires_at is not None and now >= expires_at:
            await self._delete_key(key)
            return None
        if _sha256(encoded) != digest:
            raise StorageCorruptionError(f"key/value checksum mismatch for {key!r}")
        decoded = json.loads(encoded)
        if not isinstance(decoded, dict) or _canonical_json(decoded) != encoded:
            raise StorageCorruptionError(f"key/value JSON is invalid for {key!r}")
        return dict(decoded)

    async def set_json(
        self,
        key: str,
        value: dict[str, Any],
        ttl_seconds: int | None = None,
    ) -> None:
        if ttl_seconds is not None and ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        await self._ready()
        encoded = _canonical_json(value)
        expires_at = (
            self._clock.unix_ms() + ttl_seconds * 1_000 if ttl_seconds is not None else None
        )
        await self._upsert(
            key,
            value_kind="json",
            encoded=encoded,
            expires_at_unix_ms=expires_at,
        )

    async def increment(self, key: str) -> int:
        await self._ready()
        now = self._clock.unix_ms()

        def increment_sync(connection: Any) -> int:
            row = connection.execute(
                "SELECT value_kind, value_json, expires_at_unix_ms "
                "FROM key_values WHERE namespace=? AND key=?",
                (self._namespace, key),
            ).fetchone()
            current = 0
            if row is not None and (
                row["expires_at_unix_ms"] is None or now < int(row["expires_at_unix_ms"])
            ):
                if str(row["value_kind"]) != "integer":
                    raise ValueError(f"key {key!r} does not contain an integer")
                current = int(json.loads(str(row["value_json"])))
            result = current + 1
            encoded = str(result)
            connection.execute(
                "INSERT INTO key_values(namespace, key, value_kind, value_json, "
                "value_sha256, expires_at_unix_ms, updated_at_unix_ms) "
                "VALUES (?, ?, 'integer', ?, ?, NULL, ?) "
                "ON CONFLICT(namespace, key) DO UPDATE SET "
                "value_kind='integer', value_json=excluded.value_json, "
                "value_sha256=excluded.value_sha256, expires_at_unix_ms=NULL, "
                "updated_at_unix_ms=excluded.updated_at_unix_ms",
                (self._namespace, key, encoded, _sha256(encoded), now),
            )
            return result

        return await self._database.transaction(increment_sync)

    async def get_float(self, key: str) -> float | None:
        await self._ready()
        now = self._clock.unix_ms()

        def read_float(connection: Any) -> tuple[str, str, int | None] | None:
            row = connection.execute(
                "SELECT value_json, value_sha256, expires_at_unix_ms "
                "FROM key_values WHERE namespace=? AND key=? AND value_kind='float'",
                (self._namespace, key),
            ).fetchone()
            if row is None:
                return None
            return (
                str(row["value_json"]),
                str(row["value_sha256"]),
                int(row["expires_at_unix_ms"]) if row["expires_at_unix_ms"] is not None else None,
            )

        stored = await self._database.read(read_float)
        if stored is None:
            return None
        encoded, digest, expires_at = stored
        if expires_at is not None and now >= expires_at:
            await self._delete_key(key)
            return None
        if _sha256(encoded) != digest:
            raise StorageCorruptionError(f"float checksum mismatch for {key!r}")
        value = float(json.loads(encoded))
        if not math.isfinite(value):
            raise StorageCorruptionError(f"stored float is not finite for {key!r}")
        return value

    async def set_float(self, key: str, value: float) -> None:
        if not math.isfinite(value):
            raise ValueError("stored float must be finite")
        await self._ready()
        encoded = _canonical_json(float(value))
        await self._upsert(
            key,
            value_kind="float",
            encoded=encoded,
            expires_at_unix_ms=None,
        )

    async def _upsert(
        self,
        key: str,
        *,
        value_kind: str,
        encoded: str,
        expires_at_unix_ms: int | None,
    ) -> None:
        now = self._clock.unix_ms()

        def write(connection: Any) -> None:
            connection.execute(
                "INSERT INTO key_values(namespace, key, value_kind, value_json, "
                "value_sha256, expires_at_unix_ms, updated_at_unix_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(namespace, key) DO UPDATE SET "
                "value_kind=excluded.value_kind, value_json=excluded.value_json, "
                "value_sha256=excluded.value_sha256, "
                "expires_at_unix_ms=excluded.expires_at_unix_ms, "
                "updated_at_unix_ms=excluded.updated_at_unix_ms",
                (
                    self._namespace,
                    key,
                    value_kind,
                    encoded,
                    _sha256(encoded),
                    expires_at_unix_ms,
                    now,
                ),
            )

        await self._database.transaction(write)

    async def _delete_key(self, key: str) -> None:
        await self._database.transaction(
            lambda connection: connection.execute(
                "DELETE FROM key_values WHERE namespace=? AND key=?",
                (self._namespace, key),
            )
        )

    async def health_check(self) -> bool:
        report = await self._database.health()
        return bool(report.get("healthy", False))

    async def close(self) -> None:
        """Release process-local compatibility data; DB lifecycle is shared."""

        self._timeseries.clear()

    async def _ensure_legacy_migrated(self) -> None:
        if self._legacy_checked:
            return
        async with self._legacy_lock:
            if self._legacy_checked:
                return
            if self._legacy_store_path is not None and self._legacy_store_path.exists():
                await self._migrate_legacy_store(self._legacy_store_path)
            if self._legacy_consent_path is not None and self._legacy_consent_path.exists():
                await self._migrate_legacy_consent(self._legacy_consent_path)
            self._legacy_checked = True

    async def _migration_exists(self, kind: str, digest: str) -> bool:
        return await self._database.read(
            lambda connection: (
                connection.execute(
                    "SELECT 1 FROM legacy_migrations WHERE source_kind=? AND source_sha256=?",
                    (kind, digest),
                ).fetchone()
                is not None
            )
        )

    async def _backup_legacy(self, path: Path, *, kind: str, digest: str) -> Path:
        destination = (
            self._database.backup_dir / "legacy" / f"{kind}.{digest}{path.suffix or '.json'}"
        )
        copied = await asyncio.to_thread(_atomic_copy_bytes, path, destination)
        if copied != digest:
            raise StorageCorruptionError(f"legacy {kind} backup checksum mismatch")
        return destination

    async def _record_skipped_legacy(
        self,
        path: Path,
        *,
        kind: str,
        digest: str,
        backup: Path,
        diagnostic_code: str,
    ) -> None:
        await self._database.transaction(
            lambda connection: connection.execute(
                "INSERT INTO legacy_migrations("
                "source_kind, source_name, source_sha256, backup_name, "
                "backup_sha256, imported_records, skipped_records, status, "
                "diagnostic_code, imported_at_unix_ms"
                ") VALUES (?, ?, ?, ?, ?, 0, 1, 'skipped', ?, ?) "
                "ON CONFLICT(source_kind, source_sha256) DO NOTHING",
                (
                    kind,
                    path.name,
                    digest,
                    backup.name,
                    digest,
                    diagnostic_code,
                    self._clock.unix_ms(),
                ),
            )
        )
        logger.warning(
            "Skipped backed-up legacy %s source %s (%s)",
            kind,
            path.name,
            diagnostic_code,
        )

    async def _migrate_legacy_store(self, path: Path) -> None:
        raw = await asyncio.to_thread(path.read_bytes)
        digest = hashlib.sha256(raw).hexdigest()
        kind = "key_value_json"
        if await self._migration_exists(kind, digest):
            return
        backup = await self._backup_legacy(path, kind=kind, digest=digest)
        try:
            document = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            await self._record_skipped_legacy(
                path,
                kind=kind,
                digest=digest,
                backup=backup,
                diagnostic_code="invalid_key_value_json",
            )
            return
        if not isinstance(document, dict):
            await self._record_skipped_legacy(
                path,
                kind=kind,
                digest=digest,
                backup=backup,
                diagnostic_code="invalid_key_value_root",
            )
            return
        data = document.get("data", {})
        expiry = document.get("expiry", {})
        if not isinstance(data, dict) or not isinstance(expiry, dict):
            await self._record_skipped_legacy(
                path,
                kind=kind,
                digest=digest,
                backup=backup,
                diagnostic_code="invalid_key_value_shape",
            )
            return
        prefix = f"{self._namespace}:"
        imported: list[tuple[str, str, str, int | None]] = []
        skipped = 0
        for internal_key, value in data.items():
            if not isinstance(internal_key, str) or not internal_key.startswith(prefix):
                skipped += 1
                continue
            key = internal_key[len(prefix) :]
            if not _safe_legacy_key(key):
                skipped += 1
                continue
            if not isinstance(value, dict):
                # The current durable call sites for approved legacy keys are
                # JSON records. Numeric opaque state is not imported.
                skipped += 1
                continue
            encoded = _canonical_json(value)
            raw_expiry = expiry.get(internal_key)
            expires_at = (
                max(0, int(float(raw_expiry) * 1_000))
                if isinstance(raw_expiry, int | float)
                else None
            )
            imported.append((key, encoded, _sha256(encoded), expires_at))

        def commit(connection: Any) -> None:
            if (
                connection.execute(
                    "SELECT 1 FROM legacy_migrations WHERE source_kind=? AND source_sha256=?",
                    (kind, digest),
                ).fetchone()
                is not None
            ):
                return
            now = self._clock.unix_ms()
            for key, encoded, value_digest, expires_at in imported:
                connection.execute(
                    "INSERT INTO key_values(namespace, key, value_kind, value_json, "
                    "value_sha256, expires_at_unix_ms, updated_at_unix_ms) "
                    "VALUES (?, ?, 'json', ?, ?, ?, ?) "
                    "ON CONFLICT(namespace, key) DO NOTHING",
                    (
                        self._namespace,
                        key,
                        encoded,
                        value_digest,
                        expires_at,
                        now,
                    ),
                )
            connection.execute(
                "INSERT INTO legacy_migrations("
                "source_kind, source_name, source_sha256, backup_name, backup_sha256, "
                "imported_records, skipped_records, imported_at_unix_ms"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    kind,
                    path.name,
                    digest,
                    backup.name,
                    digest,
                    len(imported),
                    skipped,
                    now,
                ),
            )

        await self._database.transaction(commit)

    async def _migrate_legacy_consent(self, path: Path) -> None:
        raw = await asyncio.to_thread(path.read_bytes)
        digest = hashlib.sha256(raw).hexdigest()
        kind = "consent_overrides_json"
        if await self._migration_exists(kind, digest):
            return
        backup = await self._backup_legacy(path, kind=kind, digest=digest)
        try:
            document = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            await self._record_skipped_legacy(
                path,
                kind=kind,
                digest=digest,
                backup=backup,
                diagnostic_code="invalid_consent_override_json",
            )
            return
        if not isinstance(document, dict) or not isinstance(document.get("levels"), dict):
            await self._record_skipped_legacy(
                path,
                kind=kind,
                digest=digest,
                backup=backup,
                diagnostic_code="invalid_consent_override_shape",
            )
            return
        raw_global_max = document.get("global_max", 3)
        if not isinstance(raw_global_max, int) or isinstance(raw_global_max, bool):
            await self._record_skipped_legacy(
                path,
                kind=kind,
                digest=digest,
                backup=backup,
                diagnostic_code="invalid_consent_global_max",
            )
            return
        policy = {
            "levels": {
                key: max(0, min(4, value))
                for key, value in document["levels"].items()
                if isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool)
            },
            "global_max": max(0, min(4, raw_global_max)),
        }

        def commit(connection: Any) -> None:
            if (
                connection.execute(
                    "SELECT 1 FROM legacy_migrations WHERE source_kind=? AND source_sha256=?",
                    (kind, digest),
                ).fetchone()
                is not None
            ):
                return
            row = connection.execute(
                "SELECT value_json, value_sha256 FROM key_values "
                "WHERE namespace=? AND key='consent_ladder_state' AND value_kind='json'",
                (self._namespace,),
            ).fetchone()
            state: dict[str, Any]
            if row is None:
                state = {"action_states": {}, "global_max": policy["global_max"], "revision": 0}
            else:
                existing_json = str(row["value_json"])
                if _sha256(existing_json) != str(row["value_sha256"]):
                    raise StorageCorruptionError(
                        "consent ladder state checksum mismatch during migration"
                    )
                loaded = json.loads(existing_json)
                state = dict(loaded) if isinstance(loaded, dict) else {}
            state["policy"] = policy
            state["global_max"] = policy["global_max"]
            encoded = _canonical_json(state)
            now = self._clock.unix_ms()
            connection.execute(
                "INSERT INTO key_values(namespace, key, value_kind, value_json, "
                "value_sha256, expires_at_unix_ms, updated_at_unix_ms) "
                "VALUES (?, 'consent_ladder_state', 'json', ?, ?, NULL, ?) "
                "ON CONFLICT(namespace, key) DO UPDATE SET "
                "value_kind='json', value_json=excluded.value_json, "
                "value_sha256=excluded.value_sha256, expires_at_unix_ms=NULL, "
                "updated_at_unix_ms=excluded.updated_at_unix_ms",
                (self._namespace, encoded, _sha256(encoded), now),
            )
            connection.execute(
                "INSERT INTO legacy_migrations("
                "source_kind, source_name, source_sha256, backup_name, backup_sha256, "
                "imported_records, skipped_records, imported_at_unix_ms"
                ") VALUES (?, ?, ?, ?, ?, 1, 0, ?)",
                (kind, path.name, digest, backup.name, digest, now),
            )

        await self._database.transaction(commit)


__all__ = ["SQLiteKeyValueStore"]
