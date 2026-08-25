"""WP10 composition, event, command, and task-ownership contracts."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from cortex.application.coordinators import (
    ExperimentCoordinator,
    ExperimentOperations,
    InferenceCoordinator,
    InferenceOperations,
    LoopSpec,
    RuntimeCoordinatorSet,
    SensingCoordinator,
    SensingOperations,
)
from cortex.application.events import ApplicationEventHub
from cortex.application.gateway import WebSocketCommandHandlers
from cortex.application.runtime_data import RuntimeDataPort
from cortex.application.runtime_status import RuntimeStatusPort
from cortex.application.services import ServiceRegistry
from cortex.application.task_supervisor import TaskGroupName, TaskSupervisor
from cortex.services.api_gateway.app import create_app
from cortex.services.api_gateway.websocket_server import WebSocketServer


def test_service_registry_instances_are_isolated_and_app_scoped() -> None:
    first = ServiceRegistry()
    second = ServiceRegistry()
    first.register("daemon", object())

    first_app = create_app(services=first)
    second_app = create_app(services=second)

    assert first_app.state.registry is first
    assert second_app.state.registry is second
    assert first.has("daemon") is True
    assert second.has("daemon") is False


def test_runtime_data_port_has_named_current_values_and_snapshot_presence() -> None:
    data = RuntimeDataPort()
    estimate = object()
    data.publish_state_estimate(estimate)
    data.put_workspace_snapshot("intervention-1", None)

    assert data.snapshot().state_estimate is estimate
    assert data.workspace_snapshot("intervention-1") == (True, None)
    assert data.workspace_snapshot("missing") == (False, None)
    data.reset()
    assert data.snapshot().state_estimate is None


def test_event_stream_copies_payload_isolates_errors_and_unsubscribes() -> None:
    events = ApplicationEventHub()
    observed: list[dict[str, Any]] = []

    def mutating_listener(payload: dict[str, Any]) -> None:
        payload["nested"]["value"] = "changed"
        raise RuntimeError("observer failure")

    bad = events.state.subscribe(mutating_listener)
    good = events.state.subscribe(observed.append)
    source = {"nested": {"value": "original"}}

    assert events.state.publish(source) == 1
    assert source == {"nested": {"value": "original"}}
    assert observed == [{"nested": {"value": "original"}}]

    bad.cancel()
    good.cancel()
    assert events.state.subscriber_count == 0


@pytest.mark.asyncio
async def test_supervisor_reports_unexpected_critical_exit_and_drains_children() -> None:
    failures: list[Any] = []
    supervisor = TaskSupervisor(on_failure=failures.append)

    async def returns_early() -> None:
        return

    async def waits_forever() -> None:
        await asyncio.Event().wait()

    supervisor.spawn(
        returns_early(),
        name="critical-loop",
        group=TaskGroupName.INFERENCE,
        critical=True,
    )
    background = supervisor.spawn(
        waits_forever(),
        name="background-loop",
        group=TaskGroupName.BACKGROUND,
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert failures[0].exception_type == "UnexpectedTaskExit"
    snapshot = supervisor.snapshot()
    assert snapshot["groups"]["inference"][0]["state"] == "completed"
    assert snapshot["groups"]["background"][0]["state"] == "running"

    await supervisor.cancel(TaskGroupName.BACKGROUND, timeout=0.2)
    assert background.cancelled()


@pytest.mark.asyncio
async def test_coordinator_start_is_idempotent_and_restart_has_one_owner() -> None:
    supervisor = TaskSupervisor()

    async def loop() -> None:
        await asyncio.Event().wait()

    coordinator = SensingCoordinator(
        supervisor,
        (LoopSpec("capture-loop", loop),),
    )
    first = coordinator.start()
    assert coordinator.start() == first
    assert len(supervisor.tasks(TaskGroupName.SENSING)) == 1

    await coordinator.stop(timeout=0.2)
    second = coordinator.start()
    assert len(second) == 1
    assert second[0] is not first[0]
    assert len(supervisor.tasks(TaskGroupName.SENSING)) == 1
    await coordinator.stop(timeout=0.2)


@pytest.mark.asyncio
async def test_coordinator_set_rolls_back_partial_start() -> None:
    supervisor = TaskSupervisor()

    async def waits_forever() -> None:
        await asyncio.Event().wait()

    def fails_during_composition() -> Any:
        raise RuntimeError("factory failed")

    coordinators = RuntimeCoordinatorSet(
        SensingCoordinator(
            supervisor,
            (LoopSpec("capture-loop", waits_forever),),
        ),
        SensingCoordinator(
            supervisor,
            (LoopSpec("failing-loop", fails_during_composition),),
        ),
    )

    with pytest.raises(RuntimeError, match="factory failed"):
        await coordinators.start()
    assert not [task for task in supervisor.tasks() if not task.done()]


@pytest.mark.asyncio
async def test_sensing_coordinator_owns_capture_telemetry_and_context_flows() -> None:
    supervisor = TaskSupervisor()
    observed: list[str] = []
    capture_processed = asyncio.Event()

    async def next_capture() -> object:
        await asyncio.sleep(0)
        return object()

    async def process_capture(_output: object) -> None:
        observed.append("capture")
        capture_processed.set()

    async def sample_telemetry() -> None:
        observed.append("telemetry")

    async def refresh_context() -> None:
        observed.append("context")

    coordinator = SensingCoordinator(
        supervisor,
        operations=SensingOperations(
            capture_enabled=lambda: True,
            next_capture=next_capture,
            process_capture=process_capture,
            telemetry_enabled=lambda: True,
            sample_telemetry=sample_telemetry,
            refresh_context=refresh_context,
        ),
    )
    coordinator.start()
    await asyncio.wait_for(capture_processed.wait(), timeout=0.2)
    await asyncio.sleep(0)

    assert set(coordinator.task_names) == {
        "cortex-capture-loop",
        "cortex-telemetry-loop",
        "cortex-context-loop",
    }
    assert {"capture", "telemetry", "context"}.issubset(observed)
    await coordinator.stop(timeout=0.2)


@pytest.mark.asyncio
async def test_inference_coordinator_owns_steady_publication_cadence() -> None:
    supervisor = TaskSupervisor()
    published: list[tuple[object, object]] = []
    publication_seen = asyncio.Event()
    estimate = object()
    biometrics = object()

    async def state_loop() -> None:
        await asyncio.Event().wait()

    async def publish(current_estimate: object, current_biometrics: object) -> None:
        published.append((current_estimate, current_biometrics))
        publication_seen.set()

    coordinator = InferenceCoordinator(
        supervisor,
        operations=InferenceOperations(
            state_loop=state_loop,
            current_publication=lambda: (estimate, biometrics),
            publish_state=publish,
            broadcast_interval_seconds=0.001,
        ),
    )
    coordinator.start()
    await asyncio.wait_for(publication_seen.wait(), timeout=0.2)

    assert published[0] == (estimate, biometrics)
    await coordinator.stop(timeout=0.2)


def test_experiment_coordinator_declares_outcome_and_optional_diagnostics() -> None:
    async def finalize() -> None:
        return None

    async def diagnostics() -> bool:
        return False

    coordinator = ExperimentCoordinator(
        TaskSupervisor(),
        operations=ExperimentOperations(
            finalize_outcomes=finalize,
            outcome_interval_seconds=1,
            generate_diagnostics_if_due=diagnostics,
        ),
    )

    assert coordinator.task_names == (
        "cortex-policy-outcome-loop",
        "cortex-policy-diagnostics-loop",
    )


@pytest.mark.asyncio
async def test_websocket_uses_injected_services_and_publishes_without_clients() -> None:
    services = ServiceRegistry()
    daemon = object()
    services.register("daemon", daemon)
    events = ApplicationEventHub()
    observed: list[Any] = []
    events.outbound_transport.subscribe(observed.append)
    server = WebSocketServer(services=services, events=events)

    assert server._resolve_daemon() is daemon
    assert await server.send_message("SESSION_RECAP", {"session_id": "s-1"}) == 0
    assert observed[0].message_type == "SESSION_RECAP"
    assert observed[0].payload == {"session_id": "s-1"}


def test_websocket_prefers_typed_runtime_status_over_compatibility_registry() -> None:
    services = ServiceRegistry()
    services.register("daemon", object())
    expected = object()
    status = RuntimeStatusPort()
    status.bind_daemon(expected)
    server = WebSocketServer(services=services, runtime_status=status)

    assert server._resolve_daemon() is expected


def test_websocket_binds_one_command_handler_bundle() -> None:
    server = WebSocketServer()

    def shutdown() -> None:
        return None

    def user_action(_payload: dict[str, Any]) -> None:
        return None

    server.bind_command_handlers(
        WebSocketCommandHandlers(
            shutdown=shutdown,
            user_action=user_action,
        )
    )

    assert server._shutdown_callback is shutdown
    assert server._user_action_callback is user_action
    assert server._settings_callback is None
