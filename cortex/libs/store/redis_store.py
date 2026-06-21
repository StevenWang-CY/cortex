"""
Redis Store Implementation

Async Redis client backed by ``redis.asyncio``.  Falls back to
:class:`~cortex.libs.store.memory_store.InMemoryStore` when Redis is
unreachable.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, cast

import redis.asyncio as aioredis

from cortex.libs.store.memory_store import InMemoryStore

logger = logging.getLogger(__name__)

#: Redis exceptions that indicate the connection itself is gone (as
#: opposed to a logical/data error like a bad command). On any of these
#: the store drops the live client and degrades to the in-memory
#: fallback. ``TimeoutError`` here is ``redis.asyncio.TimeoutError``
#: (a ``RedisError`` subclass), not the builtin.
_CONNECTION_ERRORS: tuple[type[BaseException], ...] = (
    aioredis.ConnectionError,
    aioredis.TimeoutError,
)


class RedisStore:
    """Async Redis-backed key-value / timeseries store.

    On construction the store attempts to connect to Redis.  If the connection
    fails, all operations are transparently delegated to an
    :class:`InMemoryStore` fallback so that the rest of the system keeps
    running.

    All keys are automatically prefixed with ``<key_prefix>:`` (default
    ``"cortex:"``).
    """

    def __init__(
        self,
        *,
        host: str = "localhost",
        port: int = 6379,
        db: int = 0,
        key_prefix: str = "cortex",
        fallback_persist_path: Path | None = None,
    ) -> None:
        """Initialise the Redis store.

        Args:
            host: Redis server hostname.
            port: Redis server port.
            db: Redis database index.
            key_prefix: Prefix prepended to every key.
            fallback_persist_path: Path the in-memory fallback persists to
                when Redis turns out to be unreachable. C7 (audit): without
                this the fallback was purely in-memory, so a Redis-enabled
                deployment that lost its Redis silently dropped consent /
                calibration state on every daemon restart. ``None`` resolves
                to :func:`cortex.libs.store._default_persist_path` lazily
                (deferred import keeps this module free of the package
                ``__init__`` import cycle).
        """
        self._host = host
        self._port = port
        self._db = db
        self._prefix = key_prefix
        self._fallback_persist_path = fallback_persist_path
        self._client: aioredis.Redis | None = None
        self._fallback: InMemoryStore | None = None

    @property
    def degraded(self) -> bool:
        """True once Redis was found unreachable and the in-memory fallback
        is in use.

        C7 (audit): the daemon mirrors this onto its ``_store_degraded``
        flag after construction so the dashboard can surface a soft
        "running without Redis — state won't survive a restart cleanly"
        hint. The fallback DOES persist to disk (see
        ``fallback_persist_path``), but ``degraded`` still signals the
        intended Redis backend is absent.

        ``False`` until the first operation has probed Redis (the connection
        is lazy); it flips to ``True`` the moment ``_get_client`` records a
        failed connection and constructs the fallback.
        """
        return self._fallback is not None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _key(self, key: str) -> str:
        """Return the prefixed key."""
        return f"{self._prefix}:{key}"

    async def _get_client(self) -> aioredis.Redis | None:
        """Return the Redis client, attempting connection on first call.

        If the connection has already failed the cached fallback store is
        used instead (returns ``None``).
        """
        if self._fallback is not None:
            return None

        if self._client is not None:
            return self._client

        try:
            client = aioredis.Redis(
                host=self._host,
                port=self._port,
                db=self._db,
                decode_responses=True,
            )
            # redis.asyncio is partially untyped: ``ping`` is annotated
            # ``Awaitable[bool] | bool`` so awaiting the union directly trips
            # mypy's [misc] check. Cast to the awaitable arm — the async
            # client always returns a coroutine here.
            await cast("Any", client.ping())
            self._client = client
            logger.info("Connected to Redis at %s:%s/%s", self._host, self._port, self._db)
            return self._client
        except Exception:
            self._build_fallback()
            return None

    def _build_fallback(self) -> InMemoryStore:
        """Construct (once) and return the persistent in-memory fallback.

        Idempotent: if a fallback already exists it is returned as-is.
        Shared by ``_get_client``'s connect-time failure arm and the
        per-op runtime-degrade path (P2-BE-REDIS-RUNTIME-DEGRADE) so both
        reach the SAME persistent store at the SAME path.
        """
        if self._fallback is not None:
            return self._fallback
        persist_path = self._fallback_persist_path
        if persist_path is None:
            # Deferred import breaks the package ``__init__`` ↔
            # ``redis_store`` import cycle. The helper is cheap and
            # only hit on the (rare) Redis-unreachable path.
            from cortex.libs.store import _default_persist_path

            persist_path = _default_persist_path()
        logger.warning(
            "Redis unavailable at %s:%s – falling back to a persistent "
            "InMemoryStore at %s",
            self._host,
            self._port,
            persist_path,
        )
        self._fallback = InMemoryStore(
            key_prefix=self._prefix,
            persist_path=persist_path,
        )
        return self._fallback

    def _degrade(self) -> InMemoryStore:
        """Drop a now-dead live client and switch to the fallback.

        P2-BE-REDIS-RUNTIME-DEGRADE: the connect-time path only built the
        fallback on the FIRST (ping) failure. After a successful ping the
        per-op calls had no exception handling, so a mid-session Redis
        death raised straight to the caller and ``degraded`` never
        flipped. This drops the stale ``_client`` and constructs the same
        persistent fallback used at connect time, then returns it so the
        in-flight op can be retried.
        """
        self._client = None
        return self._build_fallback()

    def _fallback_store(self) -> InMemoryStore:
        """Return the in-memory fallback once the Redis client has been
        determined unavailable. Raises if called when no fallback exists
        (which would indicate a programming error — callers always reach this
        only after ``_get_client`` returned ``None``).

        Replaces previous ``assert self._fallback is not None`` so the check
        survives ``python -O``.
        """
        if self._fallback is None:
            raise RuntimeError(
                "RedisStore fallback was not initialised before use"
            )
        return self._fallback

    # ------------------------------------------------------------------
    # Timeseries  (sorted sets with timestamp as score)
    # ------------------------------------------------------------------

    async def append_timeseries(self, key: str, timestamp: float, value: float) -> None:
        """Append a ``(timestamp, value)`` pair to a sorted-set timeseries.

        Args:
            key: Logical key name.
            timestamp: UNIX timestamp (used as the sorted-set score).
            value: Numeric value stored as the member (encoded as
                ``"<timestamp>:<value>"`` to guarantee uniqueness).
        """
        client = await self._get_client()
        if client is None:
            return await self._fallback_store().append_timeseries(key, timestamp, value)

        ik = self._key(key)
        member = f"{timestamp}:{value}"
        try:
            await client.zadd(ik, {member: timestamp})
        except _CONNECTION_ERRORS:
            logger.warning("Redis zadd failed mid-session; degrading to fallback")
            return await self._degrade().append_timeseries(key, timestamp, value)

    async def get_timeseries(self, key: str, window_seconds: float) -> list[tuple[float, float]]:
        """Return timeseries entries within the last *window_seconds*.

        Args:
            key: Logical key name.
            window_seconds: How far back from *now* to include.

        Returns:
            A list of ``(timestamp, value)`` tuples ordered by timestamp.
        """
        client = await self._get_client()
        if client is None:
            return await self._fallback_store().get_timeseries(key, window_seconds)

        ik = self._key(key)
        min_score = time.time() - window_seconds
        try:
            members: list[tuple[str, float]] = await client.zrangebyscore(
                ik, min=min_score, max="+inf", withscores=True
            )
        except _CONNECTION_ERRORS:
            logger.warning(
                "Redis zrangebyscore failed mid-session; degrading to fallback"
            )
            return await self._degrade().get_timeseries(key, window_seconds)
        results: list[tuple[float, float]] = []
        for member, score in members:
            parts = str(member).split(":", 1)
            if len(parts) == 2:
                results.append((score, float(parts[1])))
        return results

    # ------------------------------------------------------------------
    # JSON
    # ------------------------------------------------------------------

    async def get_json(self, key: str) -> dict[str, Any] | None:
        """Retrieve a JSON dict from Redis.

        Args:
            key: Logical key name.

        Returns:
            The stored dict, or ``None`` if missing.
        """
        client = await self._get_client()
        if client is None:
            return await self._fallback_store().get_json(key)

        try:
            raw = await client.get(self._key(key))
        except _CONNECTION_ERRORS:
            logger.warning("Redis get failed mid-session; degrading to fallback")
            return await self._degrade().get_json(key)
        if raw is None:
            return None
        return cast("dict[str, Any]", json.loads(raw))

    async def set_json(self, key: str, value: dict[str, Any], ttl_seconds: int | None = None) -> None:
        """Store a dict as a JSON string, optionally with a TTL.

        Args:
            key: Logical key name.
            value: Dict to serialize and store.
            ttl_seconds: Optional expiry in seconds.
        """
        client = await self._get_client()
        if client is None:
            return await self._fallback_store().set_json(key, value, ttl_seconds)

        ik = self._key(key)
        payload = json.dumps(value)
        try:
            if ttl_seconds is not None:
                await client.set(ik, payload, ex=ttl_seconds)
            else:
                await client.set(ik, payload)
        except _CONNECTION_ERRORS:
            logger.warning("Redis set failed mid-session; degrading to fallback")
            return await self._degrade().set_json(key, value, ttl_seconds)

    # ------------------------------------------------------------------
    # Numeric helpers
    # ------------------------------------------------------------------

    async def increment(self, key: str) -> int:
        """Atomically increment an integer counter.

        Args:
            key: Logical key name.

        Returns:
            The value after incrementing.
        """
        client = await self._get_client()
        if client is None:
            return await self._fallback_store().increment(key)

        try:
            return int(await client.incr(self._key(key)))
        except _CONNECTION_ERRORS:
            logger.warning("Redis incr failed mid-session; degrading to fallback")
            return await self._degrade().increment(key)

    async def get_float(self, key: str) -> float | None:
        """Retrieve a stored float.

        Args:
            key: Logical key name.

        Returns:
            The float value, or ``None`` if missing.
        """
        client = await self._get_client()
        if client is None:
            return await self._fallback_store().get_float(key)

        try:
            raw = await client.get(self._key(key))
        except _CONNECTION_ERRORS:
            logger.warning("Redis get_float failed mid-session; degrading to fallback")
            return await self._degrade().get_float(key)
        return float(raw) if raw is not None else None

    async def set_float(self, key: str, value: float) -> None:
        """Store a float value.

        Args:
            key: Logical key name.
            value: Float to store.
        """
        client = await self._get_client()
        if client is None:
            return await self._fallback_store().set_float(key, value)

        try:
            await client.set(self._key(key), str(value))
        except _CONNECTION_ERRORS:
            logger.warning("Redis set_float failed mid-session; degrading to fallback")
            return await self._degrade().set_float(key, value)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        """Ping Redis and return ``True`` if reachable.

        Returns:
            ``True`` when Redis responds to PING (or when using the
            in-memory fallback, which is always healthy).
        """
        client = await self._get_client()
        if client is None:
            return await self._fallback_store().health_check()

        try:
            return bool(await cast("Any", client.ping()))
        except _CONNECTION_ERRORS:
            # Runtime Redis death during a health probe: degrade so the
            # fallback (always healthy) answers and ``degraded`` flips.
            logger.warning("Redis ping failed mid-session; degrading to fallback")
            return await self._degrade().health_check()
        except Exception:
            return False

    async def close(self) -> None:
        """Close the Redis connection (or the fallback store)."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            logger.info("Redis connection closed")
        if self._fallback is not None:
            await self._fallback.close()
            self._fallback = None
