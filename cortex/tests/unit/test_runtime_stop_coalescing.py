"""Shutdown callers must await one shared cleanup operation."""

from __future__ import annotations

import asyncio
from types import MethodType

import pytest

from cortex.application.task_supervisor import TaskSupervisor
from cortex.services.runtime_daemon import CortexDaemon


@pytest.mark.asyncio
async def test_concurrent_stop_callers_coalesce_and_both_wait() -> None:
    daemon = CortexDaemon.__new__(CortexDaemon)
    daemon._stop_task = None
    daemon._task_supervisor = TaskSupervisor()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def _fake_stop_once(_self: CortexDaemon) -> None:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()

    daemon._stop_once = MethodType(_fake_stop_once, daemon)  # type: ignore[method-assign]

    first = asyncio.create_task(daemon.stop())
    await entered.wait()
    second = asyncio.create_task(daemon.stop())
    await asyncio.sleep(0)

    assert calls == 1
    assert not first.done()
    assert not second.done()

    release.set()
    await asyncio.gather(first, second)
    assert calls == 1


@pytest.mark.asyncio
async def test_repeated_stop_awaits_the_completed_shared_task() -> None:
    daemon = CortexDaemon.__new__(CortexDaemon)
    daemon._stop_task = None
    daemon._task_supervisor = TaskSupervisor()
    calls = 0

    async def _fake_stop_once(_self: CortexDaemon) -> None:
        nonlocal calls
        calls += 1

    daemon._stop_once = MethodType(_fake_stop_once, daemon)  # type: ignore[method-assign]

    await daemon.stop()
    await daemon.stop()

    assert calls == 1
