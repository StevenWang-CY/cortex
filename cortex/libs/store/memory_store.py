"""
In-Memory Store Implementation

A dictionary-backed store that implements the same interface as RedisStore.
Used as a fallback when Redis is unavailable, or for testing.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)

# Default maximum timeseries entries per key
_DEFAULT_TIMESERIES_MAXLEN = 10_000


class InMemoryStore:
    """In-memory key-value store with timeseries support.

    Uses plain dicts and deques.  Expired keys are cleaned up lazily on access.
    """

    def __init__(self, *, key_prefix: str = "cortex") -> None:
        """Initialise the in-memory store.

        Args:
            key_prefix: Prefix prepended to every key (default ``"cortex"``).
        """
        self._prefix = key_prefix
        self._data: dict[str, Any] = {}
        self._expiry: dict[str, float] = {}
        self._timeseries: dict[str, deque[tuple[float, float]]] = {}

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
            return True
        return False

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
