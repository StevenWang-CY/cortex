"""
In-Memory Store Implementation

A dictionary-backed store that implements the same interface as RedisStore.
Used as a fallback when Redis is unavailable, or for testing.

Optional file-backed persistence
--------------------------------

When the DMG ships without Redis, the daemon still needs durable
storage for a small handful of long-lived values (consent ladder state,
calibration markers). Without persistence the user's earned consent
escalations would reset on every daemon restart — a silent UX
regression the audit flagged as a P1.

Pass ``persist_path`` to the constructor to enable atomic file-backed
persistence of the JSON / scalar key-value map. Every write touches
disk via :func:`cortex.libs.utils.atomic_write.atomic_write_json`, so
a SIGKILL or disk-full mid-write leaves the prior known-good copy
intact. Timeseries entries are intentionally NOT persisted — they are
bounded by ``maxlen`` and ephemeral by design.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from pathlib import Path
from typing import Any

from cortex.libs.utils.atomic_write import atomic_write_json

logger = logging.getLogger(__name__)

# Default maximum timeseries entries per key
_DEFAULT_TIMESERIES_MAXLEN = 10_000


class InMemoryStore:
    """In-memory key-value store with timeseries support.

    Uses plain dicts and deques.  Expired keys are cleaned up lazily on access.

    When ``persist_path`` is provided every mutation of the JSON / scalar
    map writes the full ``_data`` dict to that path via
    :func:`atomic_write_json`. On construction, if the path exists, the
    on-disk dict is loaded into memory so a restart resumes the prior
    state. Timeseries entries are NOT persisted (ephemeral by design).
    """

    def __init__(
        self,
        *,
        key_prefix: str = "cortex",
        persist_path: Path | None = None,
    ) -> None:
        """Initialise the in-memory store.

        Args:
            key_prefix: Prefix prepended to every key (default ``"cortex"``).
            persist_path: Optional path for file-backed durability of the
                JSON / scalar map. When set, every ``set_json`` /
                ``set_float`` / ``increment`` writes the full ``_data``
                dict to this path atomically (mode 0o600). The expiry
                map is also persisted so TTLs survive restarts.
        """
        self._prefix = key_prefix
        self._data: dict[str, Any] = {}
        self._expiry: dict[str, float] = {}
        self._timeseries: dict[str, deque[tuple[float, float]]] = {}
        self._persist_path: Path | None = persist_path

        # Best-effort load of the prior state. A corrupted on-disk file
        # is logged and skipped rather than crashing the daemon — the
        # cost is losing prior state, not losing the ability to start.
        if persist_path is not None and persist_path.exists():
            try:
                with persist_path.open("r", encoding="utf-8") as fp:
                    blob = json.load(fp)
                if isinstance(blob, dict):
                    raw_data = blob.get("data", {})
                    raw_expiry = blob.get("expiry", {})
                    if isinstance(raw_data, dict):
                        self._data = dict(raw_data)
                    if isinstance(raw_expiry, dict):
                        # Re-coerce to float; JSON deserialisation may
                        # bring back ints.
                        self._expiry = {
                            str(k): float(v)
                            for k, v in raw_expiry.items()
                            if isinstance(v, int | float)
                        }
                    logger.info(
                        "InMemoryStore loaded persisted state from %s "
                        "(%d keys)",
                        persist_path,
                        len(self._data),
                    )
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning(
                    "InMemoryStore could not load persisted state from %s: %s",
                    persist_path,
                    exc,
                )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _key(self, key: str) -> str:
        """Return the prefixed internal key."""
        return f"{self._prefix}:{key}"

    def _is_expired(self, internal_key: str) -> bool:
        """Check whether *internal_key* has expired and evict if so."""
        exp = self._expiry.get(internal_key)
        if exp is not None and time.time() > exp:
            self._data.pop(internal_key, None)
            self._expiry.pop(internal_key, None)
            # Eviction is a mutation — sync to disk so a restart doesn't
            # resurrect the dead key.
            self._maybe_persist()
            return True
        return False

    def _maybe_persist(self) -> None:
        """Atomically flush the JSON / scalar map to ``persist_path``.

        No-op when persistence is disabled. Errors are logged at
        ``warning`` (not fatal) because losing durability is preferable
        to crashing the daemon hot path — the next successful write
        will catch the dropped state up.
        """
        path = self._persist_path
        if path is None:
            return
        try:
            atomic_write_json(
                path,
                {"data": self._data, "expiry": self._expiry},
                indent=None,
            )
            # Tighten the file mode to user-read/write only. Best-effort
            # on Windows where chmod is a no-op for non-numeric bits.
            try:
                path.chmod(0o600)
            except OSError:
                logger.debug(
                    "InMemoryStore could not chmod 0600 on %s",
                    path,
                    exc_info=True,
                )
        except OSError as exc:
            logger.warning(
                "InMemoryStore persist write to %s failed: %s",
                path,
                exc,
            )

    # ------------------------------------------------------------------
    # Timeseries
    # ------------------------------------------------------------------

    async def append_timeseries(self, key: str, timestamp: float, value: float) -> None:
        """Append a ``(timestamp, value)`` pair to a timeseries key.

        Args:
            key: Logical key name (prefix is added automatically).
            timestamp: UNIX timestamp used for ordering and windowed reads.
            value: The numeric value to store.
        """
        ik = self._key(key)
        if ik not in self._timeseries:
            self._timeseries[ik] = deque(maxlen=_DEFAULT_TIMESERIES_MAXLEN)
        self._timeseries[ik].append((timestamp, value))

    async def get_timeseries(self, key: str, window_seconds: float) -> list[tuple[float, float]]:
        """Return timeseries entries within the last *window_seconds*.

        Args:
            key: Logical key name.
            window_seconds: How far back (in seconds) from *now* to include.

        Returns:
            A list of ``(timestamp, value)`` tuples ordered by timestamp.
        """
        ik = self._key(key)
        series = self._timeseries.get(ik)
        if series is None:
            return []
        cutoff = time.time() - window_seconds
        return [(ts, val) for ts, val in series if ts >= cutoff]

    # ------------------------------------------------------------------
    # JSON
    # ------------------------------------------------------------------

    async def get_json(self, key: str) -> dict | None:
        """Retrieve a JSON-serialisable dict, or ``None`` if missing/expired.

        Args:
            key: Logical key name.

        Returns:
            The stored dict or ``None``.
        """
        ik = self._key(key)
        if self._is_expired(ik):
            return None
        return self._data.get(ik)

    async def set_json(self, key: str, value: dict, ttl_seconds: int | None = None) -> None:
        """Store a dict value, optionally with a TTL.

        Args:
            key: Logical key name.
            value: Dict to store.
            ttl_seconds: Optional time-to-live in seconds.
        """
        ik = self._key(key)
        self._data[ik] = value
        if ttl_seconds is not None:
            self._expiry[ik] = time.time() + ttl_seconds
        else:
            self._expiry.pop(ik, None)
        self._maybe_persist()

    # ------------------------------------------------------------------
    # Numeric helpers
    # ------------------------------------------------------------------

    async def increment(self, key: str) -> int:
        """Atomically increment an integer counter and return the new value.

        Args:
            key: Logical key name.

        Returns:
            The value after incrementing.
        """
        ik = self._key(key)
        self._is_expired(ik)
        current = self._data.get(ik, 0)
        new_val = int(current) + 1
        self._data[ik] = new_val
        self._maybe_persist()
        return new_val

    async def get_float(self, key: str) -> float | None:
        """Retrieve a stored float, or ``None`` if missing/expired.

        Args:
            key: Logical key name.

        Returns:
            The float value or ``None``.
        """
        ik = self._key(key)
        if self._is_expired(ik):
            return None
        val = self._data.get(ik)
        return float(val) if val is not None else None

    async def set_float(self, key: str, value: float) -> None:
        """Store a float value.

        Args:
            key: Logical key name.
            value: Float to store.
        """
        ik = self._key(key)
        self._data[ik] = value
        self._maybe_persist()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        """Return ``True`` -- the in-memory store is always healthy."""
        return True

    async def close(self) -> None:
        """Clear all data (no external resources to release)."""
        self._data.clear()
        self._expiry.clear()
        self._timeseries.clear()
        logger.debug("InMemoryStore closed")
