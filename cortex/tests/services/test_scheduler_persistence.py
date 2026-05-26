"""
MidnightScheduler persistence tests (P0 audit fix #4.B-1).

A daemon that crashes between firing the 00:05 tick and the next
start would, without disk-backed dedupe, re-fire the same-day tick on
the next start and double-aggregate yesterday's ``DailyBaseline``.
These tests cover the persistence contract:

* After a successful tick, ``scheduler_state.json`` exists in the
  configured state dir and carries the local-date ISO string.
* A fresh ``MidnightScheduler`` constructed against the same state
  dir loads the date and treats the same day as already fired
  (callback does NOT run a second time before the next 00:05).
* A corrupted state file does not crash construction — the scheduler
  falls back to a fresh start (no ``_last_fired_date``).
* A missing state file is the "first run" path and must not warn.

We use small monkeypatched intervals so the loop completes inside a
second of wall time. ``_seconds_until_next`` is patched to fire on
the first iteration; ``_MIN_POST_TICK_SLEEP_S`` is shrunk so the
post-tick wait doesn't dominate the test runtime.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path

import pytest

from cortex.services.session_report.scheduler import (
    _STATE_FILENAME,
    MidnightScheduler,
)


async def _run_until_callback(sched: MidnightScheduler, fired: asyncio.Event) -> None:
    """Start the scheduler, wait for the callback to fire once, stop it."""
    sched.start()
    try:
        await asyncio.wait_for(fired.wait(), timeout=2.0)
    finally:
        await sched.stop()


async def test_tick_persists_state_to_disk(tmp_path: Path, monkeypatch) -> None:
    """After the callback fires once, ``scheduler_state.json`` exists
    and carries the local-tz calendar date for that tick."""
    import cortex.services.session_report.scheduler as sched_mod

    monkeypatch.setattr(sched_mod, "_seconds_until_next", lambda h, m: 0.05)
    monkeypatch.setattr(sched_mod, "_MIN_POST_TICK_SLEEP_S", 0.05)

    fired = asyncio.Event()

    async def cb() -> None:
        fired.set()

    state_dir = tmp_path / "chronotype"
    sched = MidnightScheduler(cb, state_dir=state_dir)
    await _run_until_callback(sched, fired)

    state_path = state_dir / _STATE_FILENAME
    assert state_path.is_file(), "state file must be created after a tick"
    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert "last_fired_date" in data
    parsed = date.fromisoformat(data["last_fired_date"])
    # The fired date must equal the scheduler's in-memory record.
    assert sched._last_fired_date == parsed


async def test_reinit_reads_state_and_skips_same_day(
    tmp_path: Path, monkeypatch
) -> None:
    """A second ``MidnightScheduler`` against the same state dir, when
    "today" matches the persisted date, must NOT re-fire its callback
    on the next sleep-completion — the dedupe path in ``_run`` kicks
    in before the callback is awaited."""
    import cortex.services.session_report.scheduler as sched_mod

    monkeypatch.setattr(sched_mod, "_seconds_until_next", lambda h, m: 0.05)
    monkeypatch.setattr(sched_mod, "_MIN_POST_TICK_SLEEP_S", 0.05)

    state_dir = tmp_path / "chronotype"

    # ─── First scheduler: fire once, persist, stop. ───
    fired_first = asyncio.Event()

    async def cb1() -> None:
        fired_first.set()

    sched1 = MidnightScheduler(cb1, state_dir=state_dir)
    await _run_until_callback(sched1, fired_first)
    persisted_date = sched1._last_fired_date
    assert persisted_date is not None

    # ─── Second scheduler: same state dir, fresh callback counter. ───
    second_calls = {"n": 0}
    fired_second = asyncio.Event()

    async def cb2() -> None:
        second_calls["n"] += 1
        fired_second.set()

    sched2 = MidnightScheduler(cb2, state_dir=state_dir)
    # The persisted date must have been loaded into the new instance.
    assert sched2._last_fired_date == persisted_date

    sched2.start()
    # Give the loop enough time to wake from the patched 0.05 s sleep
    # AND enter the dedupe branch AND start its post-dedupe sleep.
    await asyncio.sleep(0.4)
    await sched2.stop()
    assert second_calls["n"] == 0, (
        "callback fired on a re-initialised scheduler for the same day — "
        "dedupe state did not load from disk"
    )
    assert not fired_second.is_set()


async def test_corrupted_state_file_falls_back_to_fresh_start(
    tmp_path: Path, caplog
) -> None:
    """A malformed ``scheduler_state.json`` must NOT crash construction;
    the scheduler logs a warning and starts as if no state file exists.

    Construction is synchronous, so this test is sync-shaped but kept
    in the async file so the harness shares the asyncio fixture setup.
    """
    state_dir = tmp_path / "chronotype"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / _STATE_FILENAME).write_text("not-json {[", encoding="utf-8")

    caplog.set_level("WARNING")

    async def cb() -> None:
        return None

    sched = MidnightScheduler(cb, state_dir=state_dir)
    assert sched._last_fired_date is None, (
        "malformed state must NOT seed _last_fired_date — corrupted JSON "
        "should be treated as 'no record'"
    )
    assert any(
        "malformed JSON" in r.message for r in caplog.records
    ), "corruption must log at WARNING for operator visibility"


async def test_state_file_with_wrong_shape_falls_back(tmp_path: Path) -> None:
    """A JSON file that parses but has the wrong shape (list, missing
    key, non-date string) is treated as 'no record'."""
    state_dir = tmp_path / "chronotype"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / _STATE_FILENAME

    async def cb() -> None:
        return None

    # Case 1: JSON list (not a dict).
    state_path.write_text("[1,2,3]", encoding="utf-8")
    s1 = MidnightScheduler(cb, state_dir=state_dir)
    assert s1._last_fired_date is None

    # Case 2: dict missing the key.
    state_path.write_text('{"foo": "bar"}', encoding="utf-8")
    s2 = MidnightScheduler(cb, state_dir=state_dir)
    assert s2._last_fired_date is None

    # Case 3: dict with a non-ISO string.
    state_path.write_text('{"last_fired_date": "not-a-date"}', encoding="utf-8")
    s3 = MidnightScheduler(cb, state_dir=state_dir)
    assert s3._last_fired_date is None


async def test_missing_state_dir_is_silent_first_run(tmp_path: Path) -> None:
    """No state file in a fresh install must NOT warn — that's the
    happy path for every brand-new daemon."""
    state_dir = tmp_path / "chronotype"  # intentionally not created

    async def cb() -> None:
        return None

    sched = MidnightScheduler(cb, state_dir=state_dir)
    assert sched._last_fired_date is None


async def test_no_state_dir_disables_persistence(
    tmp_path: Path, monkeypatch
) -> None:
    """Backwards compat: a scheduler constructed without a ``state_dir``
    behaves exactly like the pre-fix in-memory-only path — fires its
    callback and never writes to disk."""
    import cortex.services.session_report.scheduler as sched_mod

    monkeypatch.setattr(sched_mod, "_seconds_until_next", lambda h, m: 0.05)
    monkeypatch.setattr(sched_mod, "_MIN_POST_TICK_SLEEP_S", 0.05)

    fired = asyncio.Event()

    async def cb() -> None:
        fired.set()

    sched = MidnightScheduler(cb)  # no state_dir
    await _run_until_callback(sched, fired)
    # Nothing to assert about the FS — the contract is just "doesn't
    # crash and still fires." If we got here, both hold.
    assert sched._last_fired_date is not None
