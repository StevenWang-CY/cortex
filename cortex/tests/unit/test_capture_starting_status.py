"""A camera that is still starting is not a camera that is offline.

Opening the camera takes a second or two and the face model about ten more.
Through that window every client showed "Camera offline — open Settings to
allow camera access", at every launch, to a user who had already granted the
permission.  The installed 0.6.0 build showed it on the first screenshot taken
inside the window.

The daemon now marks the window (``capture.starting``) and the clients say
"Starting camera…".  The offline guidance is kept for a capture that failed
or went away.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from cortex.apps.desktop_shell.view_models import consumer_state_view
from cortex.libs.schemas.realtime import CaptureStatus
from cortex.libs.schemas.state import SignalQuality, StateEstimate, StateScores

OFFLINE = "Camera offline — open Settings to allow camera access"
STARTING = "Starting camera…"


def _capture_payload(**capture: Any) -> dict[str, Any]:
    return {"status": "insufficient_evidence", "biometrics": {}, "capture": capture}


def _estimate() -> StateEstimate:
    return StateEstimate(
        state="UNKNOWN",
        support_state="unknown",
        status="insufficient_evidence",
        confidence=0.0,
        scores=StateScores(flow=0.0, hypo=0.0, hyper=0.0, recovery=0.0),
        evidence_coverage=0.0,
        signal_quality=SignalQuality(physio=0.0, kinematics=0.0, telemetry=0.0),
        timestamp=0.0,
        reasons=["capture_starting"],
    )


# --- what the dashboard says -------------------------------------------------


def test_a_starting_camera_is_announced_as_starting() -> None:
    view = consumer_state_view(_capture_payload(frames_flowing=False, starting=True))

    assert view.biometrics_status == STARTING
    assert view.health_message is None, "a start-up window must not raise the offline banner"


def test_no_frames_without_a_start_in_flight_is_still_offline() -> None:
    view = consumer_state_view(_capture_payload(frames_flowing=False, starting=False))

    assert view.biometrics_status == OFFLINE


def test_a_payload_without_the_flag_keeps_the_offline_guidance() -> None:
    # An older daemon does not send ``starting``.
    view = consumer_state_view(_capture_payload(frames_flowing=False))

    assert view.biometrics_status == OFFLINE


def test_a_failed_capture_is_offline_even_if_the_flag_is_set() -> None:
    view = consumer_state_view(
        _capture_payload(frames_flowing=False, stale=True, starting=True)
    )

    assert view.biometrics_status == OFFLINE
    assert view.health_message == "Camera offline — frames are not flowing"


def test_once_frames_flow_the_flag_changes_nothing() -> None:
    view = consumer_state_view(
        _capture_payload(frames_flowing=True, face_detected=False, starting=True)
    )

    assert view.biometrics_status == "Looking for your face…"


# --- what the daemon publishes -----------------------------------------------


def test_the_schema_defaults_to_not_starting() -> None:
    assert CaptureStatus().starting is False
    assert CaptureStatus.model_validate({"frames_flowing": False}).starting is False


async def _broadcast_capture(*, starting: bool, stale: bool) -> dict[str, Any]:
    from cortex.services.api_gateway import app as app_module
    from cortex.services.api_gateway.websocket_server import WebSocketServer

    server = WebSocketServer()
    sent: list[dict[str, Any]] = []

    async def _capture_send(msg: Any) -> int:
        sent.append(msg.payload)
        return 1

    server._broadcast = _capture_send  # type: ignore[assignment,method-assign]
    app_module.registry.register("capture_starting", starting)
    app_module.registry.register("capture_stale", stale)
    try:
        await server.broadcast_state(_estimate(), None)
    finally:
        app_module.registry.register("capture_starting", False)
        app_module.registry.register("capture_stale", False)
    assert sent, "broadcast_state did not emit"
    capture = sent[0]["capture"]
    assert isinstance(capture, dict)
    return capture


@pytest.mark.asyncio
async def test_a_start_in_flight_is_published_as_starting() -> None:
    capture = await _broadcast_capture(starting=True, stale=False)

    assert capture["starting"] is True
    assert capture["frames_flowing"] is False
    assert capture["stale"] is False


@pytest.mark.asyncio
async def test_nothing_in_flight_is_published_as_not_starting() -> None:
    capture = await _broadcast_capture(starting=False, stale=False)

    assert capture["starting"] is False


@pytest.mark.asyncio
async def test_a_retry_after_a_failed_start_is_published_as_starting() -> None:
    # The first start failed because the camera permission was missing.  The
    # user has just granted it and the second start is in flight: the clients
    # must not keep telling them to grant it.
    capture = await _broadcast_capture(starting=True, stale=True)

    assert capture["starting"] is True
    assert capture["stale"] is False


@pytest.mark.asyncio
async def test_a_failure_with_no_start_in_flight_is_published_as_offline() -> None:
    capture = await _broadcast_capture(starting=False, stale=True)

    assert capture["stale"] is True
    assert capture["starting"] is False


# --- the daemon's flag follows the start -------------------------------------


class _Pipeline:
    def __init__(self, outcome: str) -> None:
        self.outcome = outcome
        self.seen_during_start: bool | None = None
        self.daemon: Any = None

    async def start(self) -> None:
        self.seen_during_start = bool(self.daemon._capture_starting)
        await asyncio.sleep(0)
        if self.outcome == "fail":
            raise RuntimeError("camera unavailable")
        if self.outcome == "cancel":
            raise asyncio.CancelledError


def _daemon(outcome: str) -> tuple[Any, _Pipeline]:
    from cortex.libs.config.settings import CortexConfig
    from cortex.services.runtime_daemon import CortexDaemon

    daemon = CortexDaemon(config=CortexConfig())
    pipeline = _Pipeline(outcome)
    pipeline.daemon = daemon
    daemon._capture_pipeline = pipeline  # type: ignore[assignment]
    return daemon, pipeline


@pytest.mark.asyncio
async def test_the_flag_is_set_for_the_start_and_held_until_the_first_frame() -> None:
    daemon, pipeline = _daemon("ok")
    assert daemon._capture_starting is False

    await daemon._start_optional_hardware()

    assert pipeline.seen_during_start is True
    assert daemon._capture_stale is False
    # ``start`` has returned but no frame has been processed: frames are not
    # flowing yet, so the mark must still be up or the next state update
    # reads as offline.  The installed build showed exactly one such update.
    assert daemon._capture_starting is True
    assert daemon._services.get("capture_starting") is True


def _frame_output(*, available: bool) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(frame_meta=SimpleNamespace(frame_available=available))


async def _process_first_lines(daemon: Any, output: Any) -> None:
    """Run ``_process_capture_output`` up to the point where the mark is read."""

    class _Stop(Exception):
        pass

    def _stop(_output: Any) -> Any:
        raise _Stop

    daemon._synchronize_capture_health = lambda: None
    daemon._observation_from_output = _stop
    with pytest.raises(_Stop):
        await daemon._process_capture_output(output)


@pytest.mark.asyncio
async def test_the_first_delivered_frame_ends_the_start() -> None:
    daemon, _pipeline = _daemon("ok")
    await daemon._start_optional_hardware()
    assert daemon._capture_starting is True

    # A scheduled observation without pixels is not a frame.
    await _process_first_lines(daemon, _frame_output(available=False))
    assert daemon._capture_starting is True

    await _process_first_lines(daemon, _frame_output(available=True))
    assert daemon._capture_starting is False
    assert daemon._services.get("capture_starting") is False


@pytest.mark.asyncio
async def test_a_start_that_never_delivers_a_frame_stops_being_starting() -> None:
    from cortex.services import runtime_daemon

    daemon, _pipeline = _daemon("ok")
    await daemon._start_optional_hardware()

    for _ in range(runtime_daemon._CAPTURE_FIRST_FRAME_GRACE_TICKS):
        daemon._expire_capture_starting_hold()
        assert daemon._capture_starting is True
    daemon._expire_capture_starting_hold()

    assert daemon._capture_starting is False, (
        "a pipeline that starts and delivers nothing must be reported as offline"
    )
    assert daemon._services.get("capture_starting") is False


def test_a_start_still_in_flight_is_not_expired_by_state_updates() -> None:
    from cortex.libs.config.settings import CortexConfig
    from cortex.services import runtime_daemon
    from cortex.services.runtime_daemon import CortexDaemon

    daemon = CortexDaemon(config=CortexConfig())
    daemon._set_capture_starting(True)

    # The face model can take longer than the grace period to load.
    for _ in range(3 * runtime_daemon._CAPTURE_FIRST_FRAME_GRACE_TICKS):
        daemon._expire_capture_starting_hold()

    assert daemon._capture_starting is True


@pytest.mark.asyncio
async def test_the_flag_is_cleared_when_the_start_fails() -> None:
    daemon, pipeline = _daemon("fail")

    await daemon._start_optional_hardware()

    assert pipeline.seen_during_start is True
    assert daemon._capture_starting is False
    assert daemon._services.get("capture_starting") is False
    assert daemon._capture_stale is True, "a failed start is offline, not starting"


@pytest.mark.asyncio
async def test_the_failure_is_announced_after_the_start_is_over() -> None:
    # The producer reports a start in flight in preference to an older
    # failure, so the flag must be down before this failure is broadcast.
    daemon, _pipeline = _daemon("fail")
    flag_when_announced: list[bool] = []

    async def _record() -> None:
        flag_when_announced.append(bool(daemon._services.get("capture_starting")))

    daemon._emit_capture_stale_broadcast = _record  # type: ignore[method-assign]

    await daemon._start_optional_hardware()

    assert flag_when_announced == [False]


@pytest.mark.asyncio
async def test_the_flag_is_cleared_when_the_start_is_cancelled() -> None:
    daemon, pipeline = _daemon("cancel")

    with pytest.raises(asyncio.CancelledError):
        await daemon._start_optional_hardware()

    assert pipeline.seen_during_start is True
    assert daemon._capture_starting is False
    assert daemon._services.get("capture_starting") is False
