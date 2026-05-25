"""
MidnightScheduler unit tests (P0 §3.2).

Verifies:

* ``_seconds_until_next`` computes the right wait to the next ``HH:MM``.
* ``stop()`` interrupts the sleep cleanly (well under 1 s of slack).
* An exception inside the callback is logged & swallowed; the loop continues.

We don't have ``freezegun`` available so the scheduler is driven by a
tiny test scheduler that uses very near-future targets (200 ms / 300 ms)
to make the test loop deterministic in well under a second of wall
time. The arithmetic helper (``_seconds_until_next``) is also tested
directly without sleeping.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import pytest

from cortex.services.session_report.scheduler import (
    MidnightScheduler,
    _seconds_until_next,
)


def test_seconds_until_next_within_one_day() -> None:
    """The helper must never return more than 24 h and never less than 1 s."""
    for h in (0, 5, 12, 23):
        for m in (0, 5, 30, 59):
            s = _seconds_until_next(h, m)
            assert 1.0 <= s <= 24 * 3600 + 1.0


def test_seconds_until_next_skips_to_tomorrow_when_past(monkeypatch) -> None:
    """When the target time has already passed today, the helper must
    return the seconds-until-tomorrow's target."""
    import cortex.services.session_report.scheduler as sched

    # Capture the real datetime class BEFORE we replace it on the module
    # so the shim's ``combine`` doesn't recurse through itself.
    real_datetime = sched.datetime
    fixed_now = real_datetime.now().astimezone().replace(
        hour=12, minute=0, second=0, microsecond=0
    )

    class _Stub:
        def __getattr__(self, name: str) -> Any:
            return getattr(real_datetime, name)

        def now(self, tz: Any = None) -> datetime:  # type: ignore[override]
            return fixed_now if tz is None else fixed_now.astimezone(tz)

        def combine(self, *a: Any, **kw: Any) -> datetime:  # type: ignore[override]
            return real_datetime.combine(*a, **kw)

    monkeypatch.setattr(sched, "datetime", _Stub())
    s = _seconds_until_next(0, 5)
    # 12h05m == 43500 s. Allow ±60 s slack for tz-aware aliasing.
    assert 43500 - 60 <= s <= 43500 + 60


async def test_stop_interrupts_sleep_quickly() -> None:
    """``stop()`` while the loop is mid-sleep must drain in well under
    1 s (the implementation uses ``asyncio.wait_for`` over a stop event)."""
    fired = asyncio.Event()

    async def cb() -> None:
        fired.set()

    # Target far in the future so sleep is "long"; we'll stop it manually.
    sched = MidnightScheduler(cb, hour=12, minute=0)
    sched.start()
    # Let the loop reach its sleep.
    await asyncio.sleep(0.05)
    t0 = asyncio.get_event_loop().time()
    await sched.stop()
    elapsed = asyncio.get_event_loop().time() - t0
    assert elapsed < 1.0, f"stop() took {elapsed:.2f}s, expected < 1.0"
    # The fixed target is far in the future — callback should never have fired.
    assert not fired.is_set()


async def test_callback_exception_is_swallowed(monkeypatch, caplog) -> None:
    """A raising callback must NOT crash the scheduler task; the error
    is logged and the loop continues (next tick is gated by the daily
    dedupe so this test asserts the survival contract — the task stays
    alive and re-enters the wait loop — not a second same-day fire)."""
    fire_count = {"n": 0}

    async def cb() -> None:
        fire_count["n"] += 1
        raise RuntimeError("nightly aggregation blew up")

    # Patch ``_seconds_until_next`` so the first tick fires after ~50 ms,
    # and shrink the post-tick sleep so the scheduler returns to its
    # next-target wait quickly.
    import cortex.services.session_report.scheduler as sched_mod

    monkeypatch.setattr(sched_mod, "_seconds_until_next", lambda h, m: 0.05)
    monkeypatch.setattr(sched_mod, "_MIN_POST_TICK_SLEEP_S", 0.05)

    caplog.set_level("WARNING")
    sched = MidnightScheduler(cb)
    sched.start()
    # Wait for the first tick + post-tick sleep cycle.
    deadline = asyncio.get_event_loop().time() + 1.0
    while fire_count["n"] < 1 and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.05)
    # Give the loop a moment to re-enter wait after the exception.
    await asyncio.sleep(0.2)
    # Survival contract: the task is still running (not crashed) after
    # the callback raised; stop() can still cleanly cancel it.
    assert sched._task is not None
    assert not sched._task.done(), "scheduler task crashed instead of surviving the raise"
    await sched.stop()
    assert fire_count["n"] >= 1, f"expected ≥1 fire; got {fire_count['n']}"
    assert any(
        "midnight scheduler callback raised" in r.message
        for r in caplog.records
    ), "exception path must log"


async def test_start_is_idempotent() -> None:
    """Calling ``start()`` twice is a no-op (existing task keeps running)."""
    sched = MidnightScheduler(lambda: asyncio.sleep(0), hour=12, minute=0)
    sched.start()
    first_task = sched._task  # type: ignore[attr-defined]
    sched.start()
    assert sched._task is first_task  # type: ignore[attr-defined]
    await sched.stop()


async def test_stop_before_start_is_noop() -> None:
    """Calling ``stop()`` before ``start()`` must not raise."""
    sched = MidnightScheduler(lambda: asyncio.sleep(0))
    await sched.stop()


async def test_callback_fires_when_sleep_completes(monkeypatch) -> None:
    """Patch the sleep helper to a tiny interval and confirm the
    callback fires within the deadline."""
    import cortex.services.session_report.scheduler as sched_mod

    monkeypatch.setattr(sched_mod, "_seconds_until_next", lambda h, m: 0.05)
    fired = asyncio.Event()

    async def cb() -> None:
        fired.set()

    sched = MidnightScheduler(cb)
    sched.start()
    try:
        await asyncio.wait_for(fired.wait(), timeout=2.0)
    finally:
        await sched.stop()
    assert fired.is_set()
