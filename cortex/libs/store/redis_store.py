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

import redis.asyncio as aioredis

from cortex.libs.store.memory_store import InMemoryStore

logger = logging.getLogger(__name__)


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
    ) -> None:
        """Initialise the Redis store.

        Args:
            host: Redis server hostname.
            port: Redis server port.
            db: Redis database index.
            key_prefix: Prefix prepended to every key.
        """
        self._host = host
        self._port = port
        self._db = db
        self._prefix = key_prefix
        self._client: aioredis.Redis | None = None
        self._fallback: InMemoryStore | None = None

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
            await client.ping()
            self._client = client
            logger.info("Connected to Redis at %s:%s/%s", self._host, self._port, self._db)
            return self._client
        except Exception:
            logger.warning(
                "Redis unavailable at %s:%s – falling back to InMemoryStore",
                self._host,
                self._port,
            )
            self._fallback = InMemoryStore(key_prefix=self._prefix)
            return None

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
            assert self._fallback is not None
            return await self._fallback.append_timeseries(key, timestamp, value)

        ik = self._key(key)
        member = f"{timestamp}:{value}"
        await client.zadd(ik, {member: timestamp})

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
            assert self._fallback is not None
            return await self._fallback.get_timeseries(key, window_seconds)

        ik = self._key(key)
        min_score = time.time() - window_seconds
        members: list[tuple[str, float]] = await client.zrangebyscore(
            ik, min=min_score, max="+inf", withscores=True
        )
        results: list[tuple[float, float]] = []
        for member, score in members:
            parts = str(member).split(":", 1)
            if len(parts) == 2:
                results.append((score, float(parts[1])))
        return results

    # ------------------------------------------------------------------
    # JSON
    # ------------------------------------------------------------------

    async def get_json(self, key: str) -> dict | None:
        """Retrieve a JSON dict from Redis.

        Args:
            key: Logical key name.

        Returns:
            The stored dict, or ``None`` if missing.
        """
        client = await self._get_client()
        if client is None:
            assert self._fallback is not None
            return await self._fallback.get_json(key)

        raw = await client.get(self._key(key))
        if raw is None:
            return None
        return json.loads(raw)

    async def set_json(self, key: str, value: dict, ttl_seconds: int | None = None) -> None:
        """Store a dict as a JSON string, optionally with a TTL.

        Args:
            key: Logical key name.
            value: Dict to serialize and store.
            ttl_seconds: Optional expiry in seconds.
        """
        client = await self._get_client()
        if client is None:
            assert self._fallback is not None
            return await self._fallback.set_json(key, value, ttl_seconds)

        ik = self._key(key)
        payload = json.dumps(value)
        if ttl_seconds is not None:
            await client.set(ik, payload, ex=ttl_seconds)
        else:
            await client.set(ik, payload)

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
            assert self._fallback is not None
            return await self._fallback.increment(key)

        return await client.incr(self._key(key))

    async def get_float(self, key: str) -> float | None:
        """Retrieve a stored float.

        Args:
            key: Logical key name.

        Returns:
            The float value, or ``None`` if missing.
        """
        client = await self._get_client()
        if client is None:
            assert self._fallback is not None
            return await self._fallback.get_float(key)

        raw = await client.get(self._key(key))
        return float(raw) if raw is not None else None

    async def set_float(self, key: str, value: float) -> None:
        """Store a float value.

        Args:
            key: Logical key name.
            value: Float to store.
        """
        client = await self._get_client()
        if client is None:
            assert self._fallback is not None
            return await self._fallback.set_float(key, value)

        await client.set(self._key(key), str(value))

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
            assert self._fallback is not None
            return await self._fallback.health_check()

        try:
            return await client.ping()
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
