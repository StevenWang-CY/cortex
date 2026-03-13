"""Tests for InMemoryStore (no Redis required)."""

from __future__ import annotations

import asyncio
import time

import pytest

from cortex.libs.store.memory_store import InMemoryStore


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


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
