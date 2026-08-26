"""Structured ownership for long-lived Cortex asyncio tasks."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Callable, Coroutine
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class TaskGroupName(StrEnum):
    SENSING = "sensing"
    INFERENCE = "inference"
    INTERVENTION = "intervention"
    EXPERIMENT = "experiment"
    OPERATIONS = "operations"
    TRANSPORT = "transport"
    BACKGROUND = "background"
    LIFECYCLE = "lifecycle"


@dataclass(frozen=True, slots=True)
class TaskFailure:
    name: str
    group: str
    critical: bool
    exception_type: str
    message: str


@dataclass(frozen=True, slots=True)
class TaskRecord:
    name: str
    group: str
    critical: bool
    state: str
    failure: TaskFailure | None = None


FailureListener = Callable[[TaskFailure], None]


class TaskSupervisor:
    """Own, observe, cancel, and drain every application task.

    This is a lifecycle supervisor rather than a fire-and-forget helper. A
    task belongs to exactly one named group, unexpected failures are consumed
    immediately, and shutdown drains children within a bounded interval.
    """

    def __init__(self, *, on_failure: FailureListener | None = None) -> None:
        self._tasks: dict[asyncio.Task[Any], tuple[str, bool]] = {}
        self._failures: list[TaskFailure] = []
        self._expected_completion: set[asyncio.Task[Any]] = set()
        self._on_failure = on_failure

    def spawn(
        self,
        awaitable: Coroutine[Any, Any, Any],
        *,
        name: str,
        group: TaskGroupName | str,
        critical: bool = False,
    ) -> asyncio.Task[Any]:
        task: asyncio.Task[Any] = asyncio.create_task(awaitable, name=name)
        return self.adopt(task, group=group, critical=critical)

    def adopt(
        self,
        task: asyncio.Task[Any],
        *,
        group: TaskGroupName | str,
        critical: bool = False,
    ) -> asyncio.Task[Any]:
        if task in self._tasks:
            raise ValueError(f"task is already supervised: {task.get_name()}")
        group_name = str(group)
        self._tasks[task] = (group_name, critical)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        metadata = self._tasks.get(task)
        if (
            metadata is None
            or task.cancelled()
            or task in self._expected_completion
        ):
            return
        exception = task.exception()
        group, critical = metadata
        if exception is None and not critical:
            return
        failure = TaskFailure(
            name=task.get_name(),
            group=group,
            critical=critical,
            exception_type=(
                type(exception).__name__
                if exception is not None
                else "UnexpectedTaskExit"
            ),
            message=(
                str(exception)
                if exception is not None
                else "critical task returned before shutdown"
            ),
        )
        self._failures.append(failure)
        logger.error(
            "Supervised task failed name=%s group=%s critical=%s",
            failure.name,
            failure.group,
            failure.critical,
            exc_info=exception,
        )
        if self._on_failure is not None:
            try:
                self._on_failure(failure)
            except Exception:
                logger.exception("Task-supervisor failure listener raised")

    def tasks(self, group: TaskGroupName | str | None = None) -> tuple[asyncio.Task[Any], ...]:
        if group is None:
            return tuple(self._tasks)
        group_name = str(group)
        return tuple(task for task, (owner, _critical) in self._tasks.items() if owner == group_name)

    async def cancel(
        self,
        group: TaskGroupName | str | None = None,
        *,
        timeout: float = 5.0,
    ) -> tuple[asyncio.Task[Any], ...]:
        targets = tuple(task for task in self.tasks(group) if not task.done())
        for task in targets:
            self._expected_completion.add(task)
            task.cancel()
        if targets:
            gather = asyncio.gather(*targets, return_exceptions=True)
            try:
                await asyncio.wait_for(gather, timeout=max(0.0, timeout))
            except TimeoutError:
                logger.error(
                    "Timed out draining supervised tasks group=%s names=%s",
                    str(group) if group is not None else "all",
                    [task.get_name() for task in targets if not task.done()],
                )
        return targets

    def expect_completion(self, task: asyncio.Task[Any] | None) -> None:
        """Mark a graceful, non-cancellation exit as lifecycle-owned."""

        if task is not None:
            self._expected_completion.add(task)

    def forget_finished(self) -> None:
        finished = {task for task in self._tasks if task.done()}
        self._tasks = {task: meta for task, meta in self._tasks.items() if task not in finished}
        self._expected_completion.difference_update(finished)

    def snapshot(self) -> dict[str, Any]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for task, (group, critical) in self._tasks.items():
            if task.cancelled():
                state = "cancelled"
            elif not task.done():
                state = "running"
            elif task.exception() is None:
                state = "completed"
            else:
                state = "failed"
            failure = next(
                (candidate for candidate in reversed(self._failures) if candidate.name == task.get_name()),
                None,
            )
            groups[group].append(
                asdict(
                    TaskRecord(
                        name=task.get_name(),
                        group=group,
                        critical=critical,
                        state=state,
                        failure=failure,
                    )
                )
            )
        return {
            "groups": {name: sorted(records, key=lambda row: row["name"]) for name, records in sorted(groups.items())},
            "failures": [asdict(failure) for failure in self._failures],
        }
