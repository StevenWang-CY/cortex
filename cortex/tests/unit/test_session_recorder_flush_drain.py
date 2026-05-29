"""P2 (audit): ``SessionRecorder.flush()`` must DRAIN the queue before the
writer thread exits — the trailing session window (last user_action, the
session_report meta-event, ...) must not be lost.

Regression: the writer loop's top-of-loop ``_stop_event`` check could fire
before the records queued ahead of the flush sentinel were consumed, so a
fast stop dropped them. The fix drains whatever is still queued on exit.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path
from typing import Any

from cortex.services.runtime_daemon import SessionRecorder


def _read_lines(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_flush_drains_all_queued_records(tmp_path: Path) -> None:
    """Every appended record is written even when flush() is called
    immediately after a burst of appends (writer had no time to keep up)."""
    rec = SessionRecorder(str(tmp_path))
    n = 200
    for i in range(n):
        rec.append("evt", {"i": i})
    # Flush right away — the writer thread is almost certainly still behind.
    rec.flush(timeout=5.0)

    lines = _read_lines(rec._path)
    seen = sorted(line["payload"]["i"] for line in lines)
    assert seen == list(range(n)), (
        f"expected all {n} records drained, got {len(lines)}"
    )


def test_flush_drains_records_queued_behind_a_set_stop_event(tmp_path: Path) -> None:
    """Directly exercise the race: stop_event is ALREADY set when records
    are still queued. The writer must still drain them (no data loss)."""
    rec = SessionRecorder(str(tmp_path))
    # Stop the live writer thread cleanly first so we can drive the loop
    # state deterministically.
    rec.flush(timeout=5.0)

    # Re-arm: queue records, then set the stop event BEFORE running a fresh
    # writer-loop pass. This reproduces the "stop_event set with records
    # still queued" condition the fix guards against.
    fresh = SessionRecorder.__new__(SessionRecorder)
    fresh._path = tmp_path / "sessions" / "drain_race.jsonl"
    fresh._path.parent.mkdir(parents=True, exist_ok=True)
    fresh._queue = queue.Queue(maxsize=4096)
    fresh._stop_event = threading.Event()
    fresh._overflow_streak = 0
    fresh._overflow_seq = 0
    for i in range(50):
        fresh._queue.put_nowait(("evt", {"i": i}, time.time()))
    # The race condition: stop is set while the queue is non-empty.
    fresh._stop_event.set()

    # Run the writer loop body directly (single pass) — it must drain.
    fresh._writer_loop()

    lines = _read_lines(fresh._path)
    seen = sorted(line["payload"]["i"] for line in lines)
    assert seen == list(range(50)), (
        f"records queued behind a set stop_event were dropped: {len(lines)}/50"
    )
