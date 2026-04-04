"""
Desktop Shell — In-Process Daemon Controller

Boots the CortexDaemon in a background thread and bridges state updates
to the Qt main thread via QObject signals.  This replaces the old
``WebSocketBridge``-only approach when running as a bundled .app so that
the daemon and UI share a single TCC camera identity.

Usage (from main.py):
    controller = CortexAppController()
    sys.exit(controller.run())
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import threading
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal, Slot
from PySide6.QtWidgets import QApplication

from cortex.apps.desktop_shell.connections import ConnectionsPanel
from cortex.apps.desktop_shell.dashboard import DashboardWindow
from cortex.apps.desktop_shell.onboarding import OnboardingWindow, onboarding_marker_path
from cortex.apps.desktop_shell.overlay import OverlayWindow
from cortex.apps.desktop_shell.settings import SettingsDialog
from cortex.apps.desktop_shell.tray import CortexTrayIcon
from cortex.libs.config.settings import get_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Application Support helpers
# ---------------------------------------------------------------------------

_APP_SUPPORT = Path.home() / "Library" / "Application Support" / "Cortex"


def _ensure_storage_dirs() -> None:
    """Create ``~/Library/Application Support/Cortex/Data/`` sub-dirs."""
    data = _APP_SUPPORT / "Data"
    for sub in ("sessions", "baselines", "cache", "logs", "exports"):
        (data / sub).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# DaemonBridge — thread-safe Qt ↔ asyncio signal relay
# ---------------------------------------------------------------------------

class DaemonBridge(QObject):
    """Receives callbacks from the daemon thread and re-emits as Qt signals.

    All ``on_*`` methods are called from the **asyncio background thread**.
    Qt signal emission is inherently thread-safe (uses Qt's meta-object
    queued-connection mechanism), so the main thread receives the payload
    safely.
    """

    state_updated = Signal(dict)
    intervention_triggered = Signal(dict)
    connection_changed = Signal(bool)

    # -- callbacks invoked from daemon thread ---------------------------------

    def on_state(self, payload: dict) -> None:
        """State callback — payload is already deep-copied by the daemon."""
        self.state_updated.emit(payload)

    def on_intervention(self, payload: dict) -> None:
        """Intervention callback — payload is already deep-copied."""
        self.intervention_triggered.emit(payload)


# ---------------------------------------------------------------------------
# CortexAppController — single-process daemon + Qt UI
# ---------------------------------------------------------------------------

class CortexAppController:
    """Boots the CortexDaemon in-process and wires it to the PySide6 UI.

    The daemon's asyncio event loop runs in a **background daemon thread**.
    The Qt event loop runs on the main thread (required by macOS AppKit).
    """

    def __init__(self) -> None:
        self._config = get_config()
        self._app: QApplication | None = None
        self._tray: CortexTrayIcon | None = None
        self._dashboard: DashboardWindow | None = None
        self._connections: ConnectionsPanel | None = None
        self._overlay: OverlayWindow | None = None
        self._settings: SettingsDialog | None = None
        self._onboarding: OnboardingWindow | None = None
        self._bridge = DaemonBridge()
        self._paused = False
        self._active_intervention_id: str | None = None

        # Daemon thread state
        self._daemon: Any = None  # CortexDaemon (lazy import to avoid heavy deps at module level)
        self._daemon_loop: asyncio.AbstractEventLoop | None = None
        self._daemon_thread: threading.Thread | None = None

    # -- public API -----------------------------------------------------------

    def run(self) -> int:
        """Create the Qt app, start the daemon, and enter the event loop."""
        _ensure_storage_dirs()

        self._app = QApplication(sys.argv)
        self._app.setApplicationName("Cortex")
        self._app.setOrganizationName("Cortex")
        self._app.setQuitOnLastWindowClosed(False)

        # -- UI components ----------------------------------------------------
        self._dashboard = DashboardWindow()
        self._connections = ConnectionsPanel()
        self._overlay = OverlayWindow()
        self._settings = SettingsDialog()
        self._onboarding = OnboardingWindow()

        self._tray = CortexTrayIcon(self._app)
        self._tray.show_dashboard_requested.connect(self._show_dashboard)
        self._tray.show_connections_requested.connect(self._show_connections)
        self._tray.show_settings_requested.connect(self._show_settings)
        self._tray.pause_requested.connect(self._toggle_pause)
        self._tray.restore_requested.connect(self._restore_workspace)
        self._tray.snooze_requested.connect(self._snooze_fifteen_minutes)
        self._tray.disable_session_requested.connect(self._disable_for_session)
        self._tray.quit_requested.connect(self._quit)

        # -- Wire dashboard Connect button to connections panel ----------------
        if hasattr(self._dashboard, "_consumer") and hasattr(self._dashboard._consumer, "_connect_btn"):
            self._dashboard._consumer._connect_btn.clicked.connect(self._show_connections)

        # -- Wire bridge signals to UI ----------------------------------------
        self._bridge.state_updated.connect(self._on_state_update)
        self._bridge.intervention_triggered.connect(self._on_intervention)
        self._bridge.connection_changed.connect(self._on_connection_changed)

        self._overlay.dismissed.connect(self._on_overlay_dismissed)
        self._settings.settings_changed.connect(self._on_settings_changed)
        self._onboarding.open_settings_requested.connect(self._show_settings)
        self._onboarding.run_calibration_requested.connect(self._run_calibration)
        self._onboarding.completed.connect(self._complete_onboarding)

        # -- Start daemon in background thread --------------------------------
        self._start_daemon()

        # -- Graceful shutdown ------------------------------------------------
        self._app.aboutToQuit.connect(self._shutdown_daemon)
        signal.signal(signal.SIGINT, lambda *_: self._quit())
        signal.signal(signal.SIGTERM, lambda *_: self._quit())
        # Timer to allow Python signal handling inside Qt event loop
        _heartbeat = QTimer()
        _heartbeat.timeout.connect(lambda: None)
        _heartbeat.start(500)

        # -- Show UI ----------------------------------------------------------
        self._tray.show()
        if not onboarding_marker_path().exists():
            self._onboarding.show()
        else:
            self._show_dashboard()

        return self._app.exec()

    # -- Daemon lifecycle -----------------------------------------------------

    def _start_daemon(self) -> None:
        """Boot the CortexDaemon in a background thread."""
        # Lazy import to keep module-level imports light
        from cortex.services.runtime_daemon import CortexDaemon

        self._daemon = CortexDaemon(config=self._config)
        self._daemon.set_state_callback(self._bridge.on_state)
        self._daemon.set_intervention_callback(self._bridge.on_intervention)

        def _run() -> None:
            self._daemon_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._daemon_loop)
            try:
                self._daemon_loop.run_until_complete(self._daemon.run())
            except Exception:
                logger.exception("Daemon thread crashed")
            finally:
                self._daemon_loop.close()
                self._daemon_loop = None
            # Notify UI of disconnect
            self._bridge.connection_changed.emit(False)

        self._daemon_thread = threading.Thread(
            target=_run,
            name="cortex-daemon",
            daemon=True,  # Don't prevent exit if shutdown hangs
        )
        self._daemon_thread.start()

        # Consider connected once daemon thread is alive
        self._bridge.connection_changed.emit(True)

    def _shutdown_daemon(self) -> None:
        """Gracefully stop the daemon.  Called from Qt ``aboutToQuit``."""
        if self._daemon is None:
            return

        # Step 1: Force-release the camera so cv2.read() unblocks
        try:
            if hasattr(self._daemon, "_capture_pipeline"):
                self._daemon._capture_pipeline.release()
        except Exception:
            logger.debug("Camera release during shutdown failed (non-fatal)")

        # Step 2: Schedule async stop on the daemon's event loop
        loop = self._daemon_loop
        if loop is not None and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self._daemon.stop(), loop)
            try:
                future.result(timeout=5.0)
            except Exception:
                logger.warning("Daemon stop timed out; daemon thread is daemon=True, process will exit")

    # -- Qt slots (main thread) -----------------------------------------------

    @Slot(dict)
    def _on_state_update(self, payload: dict) -> None:
        if self._paused:
            return
        if self._dashboard is not None:
            self._dashboard.update_state(payload)
        if self._tray is not None:
            self._tray.update_state(
                payload.get("state", "FLOW"),
                payload.get("confidence", 0.0),
            )

    @Slot(dict)
    def _on_intervention(self, payload: dict) -> None:
        if self._paused:
            return
        self._active_intervention_id = payload.get("intervention_id")
        if self._overlay is not None:
            self._overlay.show_intervention(payload)

    @Slot(bool)
    def _on_connection_changed(self, connected: bool) -> None:
        if self._tray is not None:
            self._tray.set_connected(connected)
        if self._dashboard is not None:
            self._dashboard.set_connected(connected)

    @Slot(str)
    def _on_overlay_dismissed(self, intervention_id: str) -> None:
        # In-process: directly call the handler on the daemon's loop
        if self._daemon is not None and self._daemon_loop is not None:
            asyncio.run_coroutine_threadsafe(
                self._daemon._handle_user_action({
                    "action": "dismissed",
                    "intervention_id": intervention_id,
                }),
                self._daemon_loop,
            )

    @Slot(dict)
    def _on_settings_changed(self, settings: dict) -> None:
        if self._daemon is not None and self._daemon_loop is not None:
            asyncio.run_coroutine_threadsafe(
                self._daemon.apply_settings(settings),
                self._daemon_loop,
            )

    def _show_dashboard(self) -> None:
        if self._dashboard is not None:
            self._dashboard.show()
            self._dashboard.raise_()
            self._dashboard.activateWindow()

    def _show_connections(self) -> None:
        if self._connections is not None:
            self._connections.show()
            self._connections.raise_()
            self._connections.activateWindow()

    def _show_settings(self) -> None:
        if self._settings is not None:
            self._settings.show()
            self._settings.raise_()
            self._settings.activateWindow()

    def _run_calibration(self) -> None:
        import subprocess as _sp
        try:
            _sp.Popen([sys.executable, "-m", "cortex.scripts.calibrate"])
        except OSError as exc:
            logger.error("Failed to launch calibration: %s", exc)

    def _complete_onboarding(self) -> None:
        marker = onboarding_marker_path()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("completed\n")
        if self._onboarding is not None:
            self._onboarding.hide()
        self._show_dashboard()

    def _toggle_pause(self) -> None:
        self._paused = not self._paused
        if self._tray is not None:
            self._tray.set_paused(self._paused)
        if self._paused and self._overlay is not None:
            self._overlay.hide()

    def _restore_workspace(self) -> None:
        if self._active_intervention_id and self._daemon and self._daemon_loop:
            asyncio.run_coroutine_threadsafe(
                self._daemon._handle_user_action({
                    "action": "dismissed",
                    "intervention_id": self._active_intervention_id,
                }),
                self._daemon_loop,
            )

    def _snooze_fifteen_minutes(self) -> None:
        if self._daemon is not None and self._daemon_loop is not None:
            asyncio.run_coroutine_threadsafe(
                self._daemon.apply_settings({
                    "quiet_mode": True,
                    "quiet_duration_minutes": 15,
                }),
                self._daemon_loop,
            )

    def _disable_for_session(self) -> None:
        if self._daemon is not None and self._daemon_loop is not None:
            asyncio.run_coroutine_threadsafe(
                self._daemon.apply_settings({"interventions_enabled": False}),
                self._daemon_loop,
            )
        self._paused = True
        if self._tray is not None:
            self._tray.set_paused(True)
        if self._overlay is not None:
            self._overlay.hide()

    def _quit(self) -> None:
        logger.info("Shutting down Cortex desktop shell")
        if self._overlay is not None:
            self._overlay.close()
        if self._dashboard is not None:
            self._dashboard.close()
        if self._app is not None:
            self._app.quit()
