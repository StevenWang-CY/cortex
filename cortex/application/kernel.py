"""Cortex application composition root."""

from __future__ import annotations

from dataclasses import dataclass

from cortex.application.events import ApplicationEventHub
from cortex.application.runtime_data import RuntimeDataPort
from cortex.application.runtime_status import RuntimeStatusPort
from cortex.application.services import ServiceRegistry
from cortex.application.task_supervisor import FailureListener, TaskSupervisor


@dataclass(frozen=True, slots=True)
class ApplicationKernel:
    """Process-local ownership shared by transports and coordinators."""

    services: ServiceRegistry
    events: ApplicationEventHub
    tasks: TaskSupervisor
    runtime_status: RuntimeStatusPort
    runtime_data: RuntimeDataPort

    @classmethod
    def create(cls, *, on_task_failure: FailureListener | None = None) -> ApplicationKernel:
        return cls(
            services=ServiceRegistry(),
            events=ApplicationEventHub(),
            tasks=TaskSupervisor(on_failure=on_task_failure),
            runtime_status=RuntimeStatusPort(),
            runtime_data=RuntimeDataPort(),
        )
