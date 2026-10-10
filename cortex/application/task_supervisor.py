"""Structured ownership for long-lived Cortex asyncio tasks."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from collections.abc import Callable, Coroutine
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)

# Diagnostics keep values, never completed Task objects (which retain results
# and exception tracebacks). The live registry is deliberately not capped.
DIAGNOSTIC_HISTORY_LIMIT = 128


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
        self._failures: deque[TaskFailure] = deque(maxlen=DIAGNOSTIC_HISTORY_LIMIT)
        self._finished: deque[TaskRecord] = deque(maxlen=DIAGNOSTIC_HISTORY_LIMIT)
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
        metadata = self._tasks.pop(task, None)
        expected = task in self._expected_completion
        self._expected_completion.discard(task)
        if metadata is None:
            return
        group, critical = metadata
        # Even expected completion must retrieve/report a real exception.
        # Only a successful critical return is exempt during graceful stop.
        exception = None if task.cancelled() else task.exception()
        failure = None
        if exception is not None or (critical and not expected and not task.cancelled()):
            failure = TaskFailure(
                name=task.get_name(), group=group, critical=critical,
                exception_type=type(exception).__name__ if exception else "UnexpectedTaskExit",
                message=str(exception)[:2048] if exception else "critical task returned before shutdown",
            )
            self._failures.append(failure)
        self._finished.append(TaskRecord(
            name=task.get_name(), group=group, critical=critical,
            state="cancelled" if task.cancelled() else "failed" if exception else "completed",
            failure=failure,
        ))
        if failure is None:
            return
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
        current = asyncio.current_task()
        targets = tuple(
            task for task in self.tasks(group) if not task.done() and task is not current
        )
        for task in targets:
            self._expected_completion.add(task)
            task.cancel()
        if targets:
            # wait_for(gather(...)) waits for cancellation acknowledgement,
            # so a child that finishes cleanup slowly can exceed the deadline
            # indefinitely. wait returns at the deadline without disowning it.
            _done, pending = await asyncio.wait(targets, timeout=max(0.0, timeout))
            if pending:
                logger.error(
                    "Timed out draining supervised tasks group=%s names=%s",
                    str(group) if group is not None else "all",
                    sorted(task.get_name() for task in pending),
                )
        return targets

    def expect_completion(self, task: asyncio.Task[Any] | None) -> None:
        """Mark a graceful, non-cancellation exit as lifecycle-owned."""

        if task is not None and task in self._tasks:
            self._expected_completion.add(task)

    def forget_finished(self) -> None:
        # A caller may run before the event loop dispatches done callbacks.
        # Observe the outcome first instead of discarding an unread failure.
        for task in tuple(self._tasks):
            if task.done():
                self._task_done(task)

    def snapshot(self) -> dict[str, Any]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in self._finished:
            groups[record.group].append(asdict(record))
        for task, (group, critical) in self._tasks.items():
            if task.cancelled():
                state = "cancelled"
            elif not task.done():
                state = "running"
            elif task.exception() is None:
                state = "completed"
            else:
                state = "failed"
            groups[group].append(
                asdict(
                    TaskRecord(
                        name=task.get_name(),
                        group=group,
                        critical=critical,
                        state=state,
                        failure=None,
                    )
                )
            )
        return {
            "groups": {name: sorted(records, key=lambda row: row["name"]) for name, records in sorted(groups.items())},
            "failures": [asdict(failure) for failure in self._failures],
        }
