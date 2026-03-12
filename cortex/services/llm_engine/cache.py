"""
LLM Engine Response Cache

LRU cache keyed by context hash with configurable TTL. Avoids redundant LLM
calls when the workspace context hasn't meaningfully changed.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass

from cortex.libs.schemas.context import TaskContext
from cortex.libs.schemas.intervention import InterventionPlan, SimplificationConstraints
from cortex.libs.schemas.state import StateEstimate


@dataclass
class CacheEntry:
    """A single cache entry with expiration."""

    plan: InterventionPlan
    created_at: float
    ttl: float

    def is_expired(self, now: float | None = None) -> bool:
        """Check if this entry has expired."""
        if now is None:
            now = time.monotonic()
        return (now - self.created_at) > self.ttl


class LLMCache:
    """
    LRU cache for intervention plans, keyed by context hash.

    Args:
        max_size: Maximum number of entries. Oldest evicted when full.
        default_ttl: Default time-to-live in seconds (300 = 5 min).
    """

    def __init__(self, max_size: int = 64, default_ttl: float = 300.0) -> None:
        self._max_size = max_size
        self._default_ttl = default_ttl
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._hits = 0
        self._misses = 0

    def get(
        self,
        context: TaskContext,
        state: StateEstimate | None = None,
        constraints: SimplificationConstraints | None = None,
        *,
        now: float | None = None,
    ) -> InterventionPlan | None:
        """
        Look up a cached plan for the given context.

        Returns None on cache miss or expiration.
        """
        key = self._context_key(context, state, constraints)
        entry = self._cache.get(key)

        if entry is None:
            self._misses += 1
            return None

        if entry.is_expired(now):
            # Expired — remove and miss
            del self._cache[key]
            self._misses += 1
            return None

        # Cache hit — move to end (most recently used)
        self._cache.move_to_end(key)
        self._hits += 1
        return entry.plan

    def put(
        self,
        context: TaskContext,
        plan: InterventionPlan,
        state: StateEstimate | None = None,
        constraints: SimplificationConstraints | None = None,
        *,
        ttl: float | None = None,
        now: float | None = None,
    ) -> None:
        """
        Store a plan in the cache.

        If the cache is full, the least recently used entry is evicted.
        """
        key = self._context_key(context, state, constraints)
        if now is None:
            now = time.monotonic()
        if ttl is None:
            ttl = self._default_ttl

        # Remove old entry if exists (to update position)
        if key in self._cache:
            del self._cache[key]

        # Evict LRU if at capacity
        while len(self._cache) >= self._max_size:
            self._cache.popitem(last=False)

        self._cache[key] = CacheEntry(plan=plan, created_at=now, ttl=ttl)

    def invalidate(
        self,
        context: TaskContext,
        state: StateEstimate | None = None,
        constraints: SimplificationConstraints | None = None,
    ) -> bool:
        """Remove a specific entry. Returns True if it existed."""
        key = self._context_key(context, state, constraints)
        if key in self._cache:
            del self._cache[key]
            return True
        return False

    def clear(self) -> None:
        """Clear the entire cache."""
        self._cache.clear()

    @property
    def size(self) -> int:
        """Number of entries currently in the cache."""
        return len(self._cache)

    @property
    def hit_rate(self) -> float:
        """Cache hit rate (0.0 to 1.0)."""
        total = self._hits + self._misses
        if total == 0:
            return 0.0
        return self._hits / total

    @property
    def stats(self) -> dict[str, int | float]:
        """Cache statistics."""
        return {
            "size": self.size,
            "max_size": self._max_size,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": self.hit_rate,
        }

    def prune_expired(self, now: float | None = None) -> int:
        """Remove all expired entries. Returns number removed."""
        if now is None:
            now = time.monotonic()
        expired_keys = [
            k for k, v in self._cache.items() if v.is_expired(now)
        ]
        for k in expired_keys:
            del self._cache[k]
        return len(expired_keys)

    @staticmethod
    def _context_key(
        context: TaskContext,
        state: StateEstimate | None = None,
        constraints: SimplificationConstraints | None = None,
    ) -> str:
        """
        Generate a hash key from the prompt inputs.

        Context alone is not sufficient, because prompt generation also depends
        on the user's current state and any active simplification constraints.
        Timestamp and dwell are intentionally excluded from the state component
        so small temporal drift doesn't defeat caching.
        """
        state_key: dict[str, object] | None = None
        if state is not None:
            state_key = {
                "state": state.state,
                "confidence": round(state.confidence, 3),
                "signal_quality": state.signal_quality.model_dump(),
            }

        payload = {
            "context": context.model_dump(exclude_none=True),
            "state": state_key,
            "constraints": (
                constraints.model_dump(exclude_none=True)
                if constraints is not None
                else None
            ),
        }
        raw = str(payload)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]
