"""Audit F35 — retention sweep yields the event loop.

Pre-fix, ``sweep_once`` did the entire ``rglob`` walk + per-file ``stat``
+ ``unlink`` on the calling thread. Even when the daemon offloaded the
whole sync function to ``asyncio.to_thread``, the offloaded chunk held
that thread for the duration of the sweep — but worse, when callers ran
``sweep_once`` directly from the event loop (in tests or in any future
hot path), it blocked the loop entirely.

``sweep_once_async`` chunks the work at ``_FILES_PER_TICK`` files per
thread offload and yields ``await asyncio.sleep(0)`` between chunks.
The test sweeps a 5 000-file directory and concurrently runs a 100 Hz
"state loop" stub; the state coroutine must tick at least 10 times
during the sweep (would observe 0 ticks on a fully blocked loop).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from cortex.libs.config.settings import StorageConfig
from cortex.services.janitor.retention import sweep_once_async


def _make_old_files(directory: Path, count: int, mtime: float) -> None:
    """Create ``count`` files under ``directory`` with the given mtime."""
    directory.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        p = directory / f"f{i:05d}.json"
        p.write_text(f"{{\"i\": {i}}}")
        # Backdate so retention sweep deletes them.
        import os

        os.utime(p, (mtime, mtime))


@pytest.mark.asyncio
async def test_async_sweep_yields_to_event_loop(tmp_path: Path) -> None:
    """Sweep 5 000 files while a 100 Hz coroutine ticks; assert >= 10 ticks."""
    sessions_dir = tmp_path / "sessions"
    # Files are well outside the retention window (default 7 days).
    old = time.time() - 30 * 86400
    _make_old_files(sessions_dir, 5000, old)

    tick_count = 0
    state_loop_running = True

    async def state_loop() -> None:
        nonlocal tick_count
        while state_loop_running:
            tick_count += 1
            # 10 ms cadence ≈ 100 Hz; the sweep must yield often enough
            # that this coroutine progresses.
            await asyncio.sleep(0.01)

    state_task = asyncio.create_task(state_loop())

    cfg = StorageConfig(path=str(tmp_path), session_retention_days=7)
    sweep_task = asyncio.create_task(
        sweep_once_async(cfg, storage_root=tmp_path)
    )

    results = await sweep_task
    state_loop_running = False
    await state_task

    assert results["sessions"].files_deleted == 5000, (
        "expected all 5 000 backdated files to be evicted"
    )
    assert tick_count >= 10, (
        f"state loop only ticked {tick_count} times during the sweep — "
        "the sweep monopolised the event loop instead of yielding"
    )


@pytest.mark.asyncio
async def test_async_sweep_handles_empty_directory(tmp_path: Path) -> None:
    """Empty storage root: no errors, all zero counters."""
    cfg = StorageConfig(path=str(tmp_path))
    results = await sweep_once_async(cfg, storage_root=tmp_path)
    for name, r in results.items():
        assert r.files_scanned == 0, name
        assert r.files_deleted == 0, name
        assert r.errors == 0, name


@pytest.mark.asyncio
async def test_async_sweep_keeps_fresh_files(tmp_path: Path) -> None:
    """Files inside the retention window survive."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True)
    fresh = sessions_dir / "today.json"
    fresh.write_text("{}")  # now — well within 7-day window
    cfg = StorageConfig(path=str(tmp_path), session_retention_days=7)
    results = await sweep_once_async(cfg, storage_root=tmp_path)
    assert results["sessions"].files_deleted == 0
    assert fresh.exists()
