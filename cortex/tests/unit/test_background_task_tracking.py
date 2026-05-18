"""Audit F03 — background tasks are tracked and cancellable on shutdown.

The state loop previously spawned intervention dispatch tasks via bare
``asyncio.create_task(...)``. ``stop()`` cancelled only the long-running
loops listed in ``self._tasks``; any in-flight intervention task was
orphaned. If it held a file handle (session-record writer, baseline
update), the daemon could exit mid-write, truncating the JSONL.

This test exercises the new ``_spawn_background_task`` plumbing without
booting the full daemon: we monkey-construct a minimal object that has
the same ``_background_tasks`` set and the same helper, then verify
that (a) spawned tasks are tracked, (b) completion auto-discards them,
(c) cancellation drains them.
"""

from __future__ import annotations

import asyncio

import pytest


class _StubDaemon:
    """Minimal stand-in carrying just the F03 plumbing.

    Replicates the ``_background_tasks`` set + ``_spawn_background_task``
    helper from ``runtime_daemon.RuntimeDaemon`` so we can unit-test the
    behaviour without instantiating the full daemon (which requires a
    camera, store backends, etc.). The contract under test is exactly
    these few lines.
    """

    def __init__(self) -> None:
        self._background_tasks: set[asyncio.Task] = set()

    def _spawn_background_task(self, coro, *, name=None) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def stop(self) -> None:
        if self._background_tasks:
            for task in list(self._background_tasks):
                task.cancel()
            await asyncio.gather(
                *list(self._background_tasks), return_exceptions=True
            )
            self._background_tasks.clear()


@pytest.mark.asyncio
async def test_spawn_tracks_task() -> None:
    daemon = _StubDaemon()

    async def _work() -> int:
        await asyncio.sleep(0.01)
        return 42

    task = daemon._spawn_background_task(_work())
    assert task in daemon._background_tasks
    result = await task
    assert result == 42


@pytest.mark.asyncio
async def test_completed_task_auto_discards() -> None:
    daemon = _StubDaemon()

    async def _quick() -> None:
        return None

    task = daemon._spawn_background_task(_quick())
    await task
    # done callbacks run on the next tick; yield once.
    await asyncio.sleep(0)
    assert task not in daemon._background_tasks
    assert daemon._background_tasks == set()


@pytest.mark.asyncio
async def test_stop_cancels_inflight_tasks() -> None:
    daemon = _StubDaemon()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _long_running() -> None:
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    daemon._spawn_background_task(_long_running())
    await started.wait()
    assert len(daemon._background_tasks) == 1

    await daemon.stop()

    assert cancelled.is_set()
    assert daemon._background_tasks == set()


@pytest.mark.asyncio
async def test_stop_drains_multiple_concurrent_tasks() -> None:
    daemon = _StubDaemon()
    started_count = 0
    started_event = asyncio.Event()

    async def _worker(i: int) -> None:
        nonlocal started_count
        started_count += 1
        if started_count == 3:
            started_event.set()
        await asyncio.sleep(60)

    for i in range(3):
        daemon._spawn_background_task(_worker(i))

    await started_event.wait()
    assert len(daemon._background_tasks) == 3

    await daemon.stop()
    assert daemon._background_tasks == set()
