"""Tests for InMemoryStore + C7 Redis-fallback durability (no Redis required)."""

from __future__ import annotations

import asyncio
import time

import pytest

from cortex.libs.store import _default_persist_path, make_default_store
from cortex.libs.store.memory_store import InMemoryStore
from cortex.libs.store.redis_store import RedisStore


def _run(coro):
    # ``asyncio.run`` spins up a fresh event loop per call, so these
    # synchronous wrappers stay robust under pytest-asyncio AUTO mode
    # (which closes the policy loop after any ``async def`` test runs).
    # The legacy ``asyncio.get_event_loop().run_until_complete`` pattern
    # raised "Event loop is closed" once a sibling async test had run in
    # the same session.
    return asyncio.run(coro)


@pytest.fixture
def store():
    return InMemoryStore(key_prefix="test")


# ---------------------------------------------------------------------------
# get_json / set_json roundtrip
# ---------------------------------------------------------------------------

class TestJsonRoundtrip:
    def test_set_and_get(self, store):
        _run(store.set_json("key1", {"name": "alice", "score": 42}))
        result = _run(store.get_json("key1"))
        assert result == {"name": "alice", "score": 42}

    def test_get_missing_key_returns_none(self, store):
        assert _run(store.get_json("nonexistent")) is None

    def test_overwrite_value(self, store):
        _run(store.set_json("key1", {"v": 1}))
        _run(store.set_json("key1", {"v": 2}))
        assert _run(store.get_json("key1")) == {"v": 2}

    def test_nested_dict(self, store):
        data = {"a": {"b": {"c": [1, 2, 3]}}}
        _run(store.set_json("nested", data))
        assert _run(store.get_json("nested")) == data


# ---------------------------------------------------------------------------
# append_timeseries / get_timeseries
# ---------------------------------------------------------------------------

class TestTimeseries:
    def test_append_and_get(self, store):
        now = time.time()
        _run(store.append_timeseries("ts1", now - 5, 10.0))
        _run(store.append_timeseries("ts1", now - 3, 20.0))
        _run(store.append_timeseries("ts1", now - 1, 30.0))

        results = _run(store.get_timeseries("ts1", window_seconds=10))
        assert len(results) == 3
        values = [v for _, v in results]
        assert values == [10.0, 20.0, 30.0]

    def test_window_filters_old_entries(self, store):
        now = time.time()
        _run(store.append_timeseries("ts2", now - 100, 1.0))
        _run(store.append_timeseries("ts2", now - 1, 2.0))

        results = _run(store.get_timeseries("ts2", window_seconds=10))
        assert len(results) == 1
        assert results[0][1] == 2.0

    def test_empty_timeseries(self, store):
        results = _run(store.get_timeseries("empty_ts", window_seconds=60))
        assert results == []


# ---------------------------------------------------------------------------
# TTL eviction
# ---------------------------------------------------------------------------

class TestTTL:
    def test_ttl_evicts_after_expiry(self, store):
        # Set with a TTL of 1 second
        _run(store.set_json("expiring", {"temp": True}, ttl_seconds=1))
        # Should exist immediately
        assert _run(store.get_json("expiring")) is not None

        # Force expiry by manipulating internal state
        ik = store._key("expiring")
        store._expiry[ik] = time.time() - 1  # pretend it expired 1s ago

        assert _run(store.get_json("expiring")) is None

    def test_no_ttl_does_not_expire(self, store):
        _run(store.set_json("permanent", {"forever": True}))
        ik = store._key("permanent")
        assert ik not in store._expiry
        assert _run(store.get_json("permanent")) == {"forever": True}

    def test_ttl_removed_when_set_without(self, store):
        _run(store.set_json("k", {"a": 1}, ttl_seconds=60))
        ik = store._key("k")
        assert ik in store._expiry

        # Re-set without TTL should clear it
        _run(store.set_json("k", {"a": 2}))
        assert ik not in store._expiry


# ---------------------------------------------------------------------------
# increment
# ---------------------------------------------------------------------------

class TestIncrement:
    def test_increment_new_key(self, store):
        val = _run(store.increment("counter"))
        assert val == 1

    def test_increment_existing(self, store):
        _run(store.increment("counter"))
        _run(store.increment("counter"))
        val = _run(store.increment("counter"))
        assert val == 3

    def test_increment_after_expired_resets(self, store):
        _run(store.increment("exp_counter"))
        _run(store.increment("exp_counter"))
        # Force expire
        ik = store._key("exp_counter")
        store._expiry[ik] = time.time() - 1
        # After expiry, should start from 0 again
        val = _run(store.increment("exp_counter"))
        assert val == 1


# ---------------------------------------------------------------------------
# Other operations
# ---------------------------------------------------------------------------

class TestMiscellaneous:
    def test_health_check(self, store):
        assert _run(store.health_check()) is True

    def test_close_clears_data(self, store):
        _run(store.set_json("k", {"x": 1}))
        _run(store.close())
        assert _run(store.get_json("k")) is None

    def test_get_float_set_float(self, store):
        _run(store.set_float("f1", 3.14))
        assert _run(store.get_float("f1")) == pytest.approx(3.14)

    def test_get_float_missing(self, store):
        assert _run(store.get_float("missing")) is None


# ---------------------------------------------------------------------------
# Finding-9: increment atomicity across concurrent awaiters
# ---------------------------------------------------------------------------


class TestIncrementConcurrency:
    def test_concurrent_increments_lose_no_updates(self, store):
        """1000 concurrent ``increment`` coroutines must produce exactly
        1000 — no lost updates. The asyncio.Lock guards the
        read-modify-write-persist sequence so the docstring's
        "atomically" claim holds even if a future await is introduced
        inside the critical section."""

        async def hammer() -> int:
            await asyncio.gather(*(store.increment("hot") for _ in range(1000)))
            return await store.increment("hot") - 1  # final count w/o the probe

        # _run uses asyncio.run -> single fresh loop; gather schedules all
        # 1000 coroutines onto it concurrently.
        final = _run(hammer())
        assert final == 1000


# ---------------------------------------------------------------------------
# C7 (audit): Redis durability — default store + degraded fallback
# ---------------------------------------------------------------------------


class _RedisDisabledCfg:
    """Duck-typed config stub: Redis sub-config with enabled=False."""

    class _Redis:
        enabled = False
        key_prefix = "cortex"

    redis = _Redis()


class _RedisEnabledUnreachableCfg:
    """Duck-typed config stub: Redis enabled but pointed at a dead port."""

    class _Redis:
        enabled = True
        host = "127.0.0.1"
        # Port 1 is never a live Redis; the connection attempt fails fast.
        port = 1
        db = 0
        key_prefix = "cortex"

    redis = _Redis()


class TestC7DefaultStorePersists:
    def test_default_config_returns_persistent_in_memory_store(self, tmp_path):
        """C7 (a): with Redis disabled (the new default), make_default_store
        returns an InMemoryStore wired to a persist_path so consent /
        calibration state survives a daemon restart."""
        persist = tmp_path / "store.json"
        store = make_default_store(_RedisDisabledCfg(), persist_path=persist)
        assert isinstance(store, InMemoryStore)

        _run(store.set_json("consent_ladder_state", {"level": "reversible_act"}))
        assert persist.exists()

        # Simulate a daemon restart: a fresh store at the same path must
        # resume the prior value.
        reborn = make_default_store(_RedisDisabledCfg(), persist_path=persist)
        assert _run(reborn.get_json("consent_ladder_state")) == {
            "level": "reversible_act"
        }

    def test_default_persist_path_used_when_none_supplied(self):
        """No explicit persist_path -> the OS-appropriate default path is
        used (never an ephemeral in-memory-only store)."""
        store = make_default_store(_RedisDisabledCfg())
        assert isinstance(store, InMemoryStore)
        assert store._persist_path == _default_persist_path()


class TestC7RedisFallbackDurability:
    def test_enabled_but_unreachable_falls_back_and_persists(self, tmp_path):
        """C7 (b): Redis enabled-but-unreachable -> the RedisStore degrades
        to a PERSISTENT InMemoryStore. State survives a simulated restart
        and ``degraded`` is True."""
        persist = tmp_path / "redis_fallback.json"
        store = make_default_store(
            _RedisEnabledUnreachableCfg(), persist_path=persist
        )
        assert isinstance(store, RedisStore)
        # Before any op the connection is lazy, so not yet degraded.
        assert store.degraded is False

        # First op probes Redis (fails), constructs the persistent fallback.
        _run(store.set_json("consent_ladder_state", {"level": "preview"}))
        assert store.degraded is True
        assert persist.exists()

        # Simulated restart: a brand-new RedisStore at the same dead port +
        # persist path must resume the prior value from disk.
        reborn = make_default_store(
            _RedisEnabledUnreachableCfg(), persist_path=persist
        )
        assert _run(reborn.get_json("consent_ladder_state")) == {"level": "preview"}
        assert reborn.degraded is True

    def test_degraded_flag_starts_false_before_any_operation(self, tmp_path):
        store = RedisStore(
            host="127.0.0.1",
            port=1,
            fallback_persist_path=tmp_path / "fb.json",
        )
        assert store.degraded is False
