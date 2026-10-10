"""Task ownership must be bounded without losing unfinished cleanup."""

from __future__ import annotations

import asyncio
import gc
import weakref

import pytest

from cortex.application.coordinators import LoopSpec, SensingCoordinator
from cortex.application.task_supervisor import TaskGroupName, TaskSupervisor


@pytest.mark.asyncio
async def test_completed_background_results_are_released_and_history_is_bounded() -> None:
    supervisor = TaskSupervisor()

    class Payload:
        pass

    async def finishes() -> Payload:
        return Payload()

    references = []
    for index in range(400):
        task = supervisor.spawn(
            finishes(), name=f"one-shot-{index}", group=TaskGroupName.BACKGROUND,
        )
        payload = await task
        references.append(weakref.ref(payload))
    del task, payload
    await asyncio.sleep(0)
    gc.collect()

    assert supervisor.tasks() == ()
    assert all(reference() is None for reference in references)
    records = supervisor.snapshot()["groups"]["background"]
    assert 0 < len(records) <= 128
    assert records[-1]["state"] == "completed"


@pytest.mark.asyncio
async def test_expected_completion_still_reports_real_exception() -> None:
    failures = []
    supervisor = TaskSupervisor(on_failure=failures.append)

    async def fails() -> None:
        raise RuntimeError("cleanup failed")

    task = supervisor.spawn(fails(), name="cleanup", group=TaskGroupName.OPERATIONS)
    supervisor.expect_completion(task)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert failures and failures[0].exception_type == "RuntimeError"
    assert supervisor.snapshot()["groups"]["operations"][0]["state"] == "failed"


@pytest.mark.asyncio
async def test_reused_task_name_does_not_inherit_an_old_failure() -> None:
    supervisor = TaskSupervisor()

    async def fails() -> None:
        raise ValueError("old failure")

    task = supervisor.spawn(fails(), name="repeat", group=TaskGroupName.BACKGROUND)
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)
    active = supervisor.spawn(
        asyncio.Event().wait(), name="repeat", group=TaskGroupName.BACKGROUND,
    )
    try:
        running = [row for row in supervisor.snapshot()["groups"]["background"]
                   if row["state"] == "running"]
        assert len(running) == 1
        assert running[0]["failure"] is None
    finally:
        await supervisor.cancel(timeout=0.1)
        assert active.done()


@pytest.mark.asyncio
async def test_cancel_deadline_retains_survivor_and_prevents_duplicate_restart() -> None:
    supervisor = TaskSupervisor()
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_cleanup() -> None:
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                # Models an external adapter finishing a mandatory cleanup.
                continue

    coordinator = SensingCoordinator(supervisor, (LoopSpec("slow", slow_cleanup),))
    original = coordinator.start()[0]
    await started.wait()
    stop = asyncio.create_task(coordinator.stop(timeout=0.01))
    try:
        # A second independent deadline avoids hanging the test on the defect.
        done, _ = await asyncio.wait({stop}, timeout=0.2)
        assert stop in done, "supervisor cancellation waited past its deadline"
        assert supervisor.tasks() == (original,)
        assert coordinator.start() == (original,)
    finally:
        release.set()
        await asyncio.gather(original, stop, return_exceptions=True)
        await asyncio.sleep(0)
        supervisor.forget_finished()
