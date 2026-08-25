"""Bounded coordinators for Cortex's long-running application flows."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from cortex.application.gateway import WebSocketCommandHandlers
from cortex.application.task_supervisor import TaskGroupName, TaskSupervisor

CoroutineFactory = Callable[[], Coroutine[Any, Any, Any]]
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LoopSpec:
    name: str
    factory: CoroutineFactory
    critical: bool = True


class FlowCoordinator:
    """Own the lifecycle of a cohesive set of application loops."""

    group: TaskGroupName

    def __init__(self, supervisor: TaskSupervisor, loops: Iterable[LoopSpec]) -> None:
        self._supervisor = supervisor
        self._loops = tuple(loops)
        names = [loop.name for loop in self._loops]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate task names in {self.group}: {names}")
        self._started = False

    def start(self) -> tuple[Any, ...]:
        if self._started:
            return self._supervisor.tasks(self.group)
        # Mark ownership before constructing children. If a later factory
        # raises, RuntimeCoordinatorSet can still stop this coordinator and
        # drain the already-created prefix.
        self._started = True
        tasks: list[Any] = []
        for loop in self._loops:
            tasks.append(self._supervisor.spawn(
                loop.factory(),
                name=loop.name,
                group=self.group,
                critical=loop.critical,
            ))
        return tuple(tasks)

    async def stop(self, *, timeout: float = 5.0) -> None:
        await self._supervisor.cancel(self.group, timeout=timeout)
        self._supervisor.forget_finished()
        self._started = False

    @property
    def task_names(self) -> tuple[str, ...]:
        return tuple(loop.name for loop in self._loops)


@dataclass(frozen=True, slots=True)
class SensingOperations:
    capture_enabled: Callable[[], bool]
    next_capture: Callable[[], Awaitable[Any | None]]
    process_capture: Callable[[Any], Awaitable[None]]
    telemetry_enabled: Callable[[], bool]
    sample_telemetry: Callable[[], Awaitable[None]]
    refresh_context: Callable[[], Awaitable[None]]


class SensingCoordinator(FlowCoordinator):
    group = TaskGroupName.SENSING

    def __init__(
        self,
        supervisor: TaskSupervisor,
        loops: Iterable[LoopSpec] = (),
        *,
        operations: SensingOperations | None = None,
    ) -> None:
        self._operations = operations
        owned_loops = loops
        if operations is not None:
            owned_loops = (
                LoopSpec("cortex-capture-loop", self._capture_loop),
                LoopSpec("cortex-telemetry-loop", self._telemetry_loop),
                LoopSpec("cortex-context-loop", self._context_loop),
            )
        super().__init__(supervisor, owned_loops)

    def _require_operations(self) -> SensingOperations:
        if self._operations is None:
            raise RuntimeError("sensing operations are not composed")
        return self._operations

    async def _capture_loop(self) -> None:
        operations = self._require_operations()
        while True:
            if not operations.capture_enabled():
                await asyncio.sleep(0.5)
                continue
            try:
                output = await operations.next_capture()
                if output is not None:
                    await operations.process_capture(output)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Capture sensing iteration failed; continuing")
                await asyncio.sleep(0.5)

    async def _telemetry_loop(self) -> None:
        operations = self._require_operations()
        while True:
            try:
                if operations.telemetry_enabled():
                    await operations.sample_telemetry()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Telemetry sensing iteration failed; continuing")
            await asyncio.sleep(0.5)

    async def _context_loop(self) -> None:
        operations = self._require_operations()
        while True:
            try:
                await operations.refresh_context()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Context sensing iteration failed; continuing")
            await asyncio.sleep(5.0)


@dataclass(frozen=True, slots=True)
class InferenceOperations:
    state_loop: CoroutineFactory
    current_publication: Callable[[], tuple[Any, Any] | None]
    publish_state: Callable[[Any, Any], Awaitable[Any]]
    broadcast_interval_seconds: float = 0.5


class InferenceCoordinator(FlowCoordinator):
    group = TaskGroupName.INFERENCE

    def __init__(
        self,
        supervisor: TaskSupervisor,
        loops: Iterable[LoopSpec] = (),
        *,
        operations: InferenceOperations | None = None,
    ) -> None:
        self._operations = operations
        owned_loops = loops
        if operations is not None:
            if operations.broadcast_interval_seconds <= 0:
                raise ValueError("broadcast interval must be positive")
            owned_loops = (
                LoopSpec("cortex-state-loop", operations.state_loop),
                LoopSpec("cortex-broadcast-loop", self._broadcast_loop),
            )
        super().__init__(supervisor, owned_loops)

    async def _broadcast_loop(self) -> None:
        operations = self._operations
        if operations is None:
            raise RuntimeError("inference operations are not composed")
        while True:
            await asyncio.sleep(operations.broadcast_interval_seconds)
            publication = operations.current_publication()
            if publication is None:
                continue
            try:
                await operations.publish_state(*publication)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("State publication failed; continuing")


class CommandGateway(Protocol):
    def bind_command_handlers(self, handlers: WebSocketCommandHandlers) -> None: ...


class InterventionCoordinator(FlowCoordinator):
    """Own event-driven application commands as well as intervention tasks."""

    group = TaskGroupName.INTERVENTION

    def __init__(
        self,
        supervisor: TaskSupervisor,
        loops: Iterable[LoopSpec] = (),
        *,
        handlers: WebSocketCommandHandlers | None = None,
    ) -> None:
        super().__init__(supervisor, loops)
        self._handlers = handlers or WebSocketCommandHandlers()

    @property
    def handlers(self) -> WebSocketCommandHandlers:
        return self._handlers

    def bind(self, gateway: CommandGateway) -> None:
        gateway.bind_command_handlers(self._handlers)


@dataclass(frozen=True, slots=True)
class ExperimentOperations:
    finalize_outcomes: Callable[[], Awaitable[None]]
    outcome_interval_seconds: float
    generate_diagnostics_if_due: Callable[[], Awaitable[bool]] | None = None
    diagnostics_poll_seconds: float = 60.0
    diagnostics_post_run_seconds: float = 300.0


class ExperimentCoordinator(FlowCoordinator):
    group = TaskGroupName.EXPERIMENT

    def __init__(
        self,
        supervisor: TaskSupervisor,
        loops: Iterable[LoopSpec] = (),
        *,
        operations: ExperimentOperations | None = None,
    ) -> None:
        self._operations = operations
        owned_loops = loops
        if operations is not None:
            if operations.outcome_interval_seconds <= 0:
                raise ValueError("outcome interval must be positive")
            composed = [LoopSpec("cortex-policy-outcome-loop", self._outcome_loop)]
            if operations.generate_diagnostics_if_due is not None:
                composed.append(
                    LoopSpec(
                        "cortex-policy-diagnostics-loop",
                        self._diagnostics_loop,
                        critical=False,
                    )
                )
            owned_loops = composed
        super().__init__(supervisor, owned_loops)

    def _require_operations(self) -> ExperimentOperations:
        if self._operations is None:
            raise RuntimeError("experiment operations are not composed")
        return self._operations

    async def _outcome_loop(self) -> None:
        operations = self._require_operations()
        while True:
            try:
                await operations.finalize_outcomes()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Policy outcome finalization failed")
            await asyncio.sleep(operations.outcome_interval_seconds)

    async def _diagnostics_loop(self) -> None:
        operations = self._require_operations()
        generate = operations.generate_diagnostics_if_due
        if generate is None:
            return
        while True:
            try:
                generated = await generate()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Policy diagnostics generation failed")
                generated = False
            await asyncio.sleep(
                operations.diagnostics_post_run_seconds
                if generated
                else operations.diagnostics_poll_seconds
            )


class OperationsCoordinator(FlowCoordinator):
    group = TaskGroupName.OPERATIONS


class RuntimeCoordinatorSet:
    """Composition-owned coordinator collection with ordered teardown."""

    def __init__(self, *coordinators: FlowCoordinator) -> None:
        self._coordinators = tuple(coordinators)

    async def start(self) -> tuple[Any, ...]:
        started: list[FlowCoordinator] = []
        tasks: list[Any] = []
        try:
            for coordinator in self._coordinators:
                # Append first so a partially-started coordinator is included
                # in rollback if one of its factories raises.
                started.append(coordinator)
                tasks.extend(coordinator.start())
        except Exception:
            for coordinator in reversed(started):
                await coordinator.stop(timeout=5.0)
            raise
        return tuple(tasks)

    async def stop(self, *, timeout: float = 5.0) -> None:
        for coordinator in reversed(self._coordinators):
            await coordinator.stop(timeout=timeout)

    def ownership(self) -> dict[str, tuple[str, ...]]:
        return {
            str(coordinator.group): coordinator.task_names
            for coordinator in self._coordinators
        }
