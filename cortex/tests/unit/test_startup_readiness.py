"""Regression coverage for truthful, hardware-independent desktop startup."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_api_readiness_wait_requires_uvicorn_started_flag() -> None:
    from cortex.services.runtime_daemon import CortexDaemon

    daemon = CortexDaemon.__new__(CortexDaemon)
    daemon.config = SimpleNamespace(
        api=SimpleNamespace(host="127.0.0.1", port=9472)
    )
    daemon._uvicorn_server = SimpleNamespace(started=True)
    daemon._api_task = None
    daemon._shutdown = asyncio.Event()

    await daemon._wait_for_api_server_ready(timeout_seconds=0.1)


@pytest.mark.asyncio
async def test_api_readiness_wait_fails_when_server_task_exits() -> None:
    from cortex.services.runtime_daemon import CortexDaemon

    async def _failed_server() -> None:
        raise OSError("synthetic bind failure")

    daemon = CortexDaemon.__new__(CortexDaemon)
    daemon.config = SimpleNamespace(
        api=SimpleNamespace(host="127.0.0.1", port=9472)
    )
    daemon._uvicorn_server = SimpleNamespace(started=False)
    daemon._api_task = asyncio.create_task(_failed_server())
    daemon._shutdown = asyncio.Event()
    await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="HTTP API failed during startup"):
        await daemon._wait_for_api_server_ready(timeout_seconds=0.1)


@pytest.mark.asyncio
async def test_daemon_core_becomes_ready_while_optional_capture_is_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unresolved camera can never gate API/WS/core readiness."""

    storage = tmp_path / "storage"
    storage.mkdir()
    monkeypatch.setenv("CORTEX_STORAGE__PATH", str(storage))
    monkeypatch.delenv("CORTEX_HEADLESS_STARTUP", raising=False)

    from cortex.libs.config import settings as settings_mod
    from cortex.libs.config.settings import get_config
    from cortex.services import runtime_daemon as runtime_mod

    if hasattr(settings_mod.get_config, "cache_clear"):
        settings_mod.get_config.cache_clear()  # type: ignore[attr-defined]

    daemon = runtime_mod.CortexDaemon(config=get_config())
    capture_started = asyncio.Event()
    keep_capture_pending = asyncio.Event()

    async def _pending_capture_start() -> None:
        capture_started.set()
        await keep_capture_pending.wait()

    daemon._capture_pipeline.start = _pending_capture_start  # type: ignore[method-assign]
    daemon._database.start = AsyncMock(return_value=None)
    daemon._legacy_data_migrator.migrate_all = AsyncMock(return_value=None)
    daemon._legacy_data_migrator.load_active_calibration = AsyncMock(
        return_value=None
    )
    daemon._policy_lifecycle.start = AsyncMock(return_value=None)
    daemon._analytics_writer.start = AsyncMock(return_value=None)
    daemon._install_loop_signal_handlers = MagicMock()
    daemon._ws_server.start = AsyncMock(return_value=True)
    daemon._transaction_coordinator.recover_unfinished = AsyncMock(
        return_value=[]
    )
    daemon._start_api_server = MagicMock()
    daemon._wait_for_api_server_ready = AsyncMock(return_value=None)
    daemon._coordinators.start = AsyncMock(return_value=())
    daemon._check_morning_briefing = AsyncMock(return_value=None)
    daemon._input_hooks.start = MagicMock(return_value=True)
    daemon._window_tracker.start = MagicMock(return_value=True)

    class _Scheduler:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def start(self) -> None:
            pass

    with (
        patch.object(runtime_mod, "configure_logging"),
        patch.object(runtime_mod, "migrate_legacy_causal_report_names"),
        patch.object(runtime_mod, "MidnightScheduler", _Scheduler),
        patch("cortex.libs.auth.load_or_create_token"),
    ):
        start_task = asyncio.create_task(daemon.start())
        await asyncio.wait_for(capture_started.wait(), timeout=1.0)

        deadline = asyncio.get_running_loop().time() + 1.0
        while not daemon.is_ready and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)

        assert daemon.is_ready is True
        assert start_task.done() is False
        assert keep_capture_pending.is_set() is False

        daemon._shutdown.set()
        await asyncio.wait_for(start_task, timeout=1.0)
        await daemon._task_supervisor.cancel("background", timeout=1.0)


def test_controller_waits_for_daemon_readiness_before_connected_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spawned thread alone must never paint the UI as Connected."""

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from cortex.apps.desktop_shell.controller import CortexAppController
    from cortex.services import runtime_daemon as runtime_mod

    allow_ready = threading.Event()
    allow_exit = threading.Event()

    class _Subscription:
        def cancel(self) -> None:
            pass

    class _FakeDaemon:
        def __init__(self, *, config) -> None:
            self.config = config
            self.is_ready = False

        def subscribe_state(self, _listener):
            return _Subscription()

        def subscribe_intervention(self, _listener):
            return _Subscription()

        def set_break_overlay_ui_handler(self, _handler) -> None:
            pass

        def set_desktop_focus_probe(self, _probe) -> None:
            pass

        async def run(self) -> None:
            while not allow_ready.is_set():
                await asyncio.sleep(0.005)
            self.is_ready = True
            while not allow_exit.is_set():
                await asyncio.sleep(0.005)

    class _Signal:
        def __init__(self) -> None:
            self.values: list[bool] = []

        def emit(self, value: bool) -> None:
            self.values.append(value)

    connection_signal = _Signal()
    bridge = SimpleNamespace(
        connection_changed=connection_signal,
        on_state=lambda _payload: None,
        on_intervention=lambda _payload: None,
        on_error=MagicMock(),
    )
    controller = CortexAppController.__new__(CortexAppController)
    controller._config = MagicMock()
    controller._tray = MagicMock()
    controller._dashboard = MagicMock()
    controller._bridge = bridge
    controller._daemon = None
    controller._daemon_loop = None
    controller._daemon_thread = None
    controller._state_subscription = None
    controller._intervention_subscription = None
    controller._outbound_subscription = None
    controller._install_ws_broadcast_observer = lambda: None  # type: ignore[method-assign]

    monkeypatch.setattr(runtime_mod, "CortexDaemon", _FakeDaemon)
    controller._start_daemon()

    assert connection_signal.values == []
    controller._tray.set_starting.assert_called_once_with()
    controller._dashboard.set_starting.assert_called_once_with()

    allow_ready.set()
    deadline = time.monotonic() + 1.0
    while True not in connection_signal.values and time.monotonic() < deadline:
        time.sleep(0.01)
    assert connection_signal.values == [True]

    allow_exit.set()
    assert controller._daemon_thread is not None
    controller._daemon_thread.join(timeout=1.0)
    assert connection_signal.values == [True, False]
    bridge.on_error.assert_not_called()


def test_controller_surfaces_startup_failure_without_false_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-readiness daemon crash stays visible and never claims success."""

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from cortex.apps.desktop_shell.controller import CortexAppController
    from cortex.services import runtime_daemon as runtime_mod

    class _Subscription:
        def cancel(self) -> None:
            pass

    class _FailingDaemon:
        def __init__(self, *, config) -> None:
            self.config = config
            self.is_ready = False

        def subscribe_state(self, _listener):
            return _Subscription()

        def subscribe_intervention(self, _listener):
            return _Subscription()

        def set_break_overlay_ui_handler(self, _handler) -> None:
            pass

        def set_desktop_focus_probe(self, _probe) -> None:
            pass

        async def run(self) -> None:
            raise RuntimeError("synthetic startup failure")

    class _Signal:
        def __init__(self) -> None:
            self.values: list[bool] = []

        def emit(self, value: bool) -> None:
            self.values.append(value)

    connection_signal = _Signal()
    bridge = SimpleNamespace(
        connection_changed=connection_signal,
        on_state=lambda _payload: None,
        on_intervention=lambda _payload: None,
        on_error=MagicMock(),
    )
    controller = CortexAppController.__new__(CortexAppController)
    controller._config = MagicMock()
    controller._tray = MagicMock()
    controller._dashboard = MagicMock()
    controller._bridge = bridge
    controller._daemon = None
    controller._daemon_loop = None
    controller._daemon_thread = None
    controller._state_subscription = None
    controller._intervention_subscription = None
    controller._outbound_subscription = None
    controller._install_ws_broadcast_observer = lambda: None  # type: ignore[method-assign]

    monkeypatch.setattr(runtime_mod, "CortexDaemon", _FailingDaemon)
    controller._start_daemon()

    assert controller._daemon_thread is not None
    controller._daemon_thread.join(timeout=1.0)
    assert connection_signal.values == [False]
    bridge.on_error.assert_called_once_with(
        "Cortex couldn't start",
        "Core services stopped before becoming ready. "
        "Diagnostic details were saved to the Cortex startup log.",
        "RuntimeError",
    )
