"""
Desktop Shell — Main Application Entry

PySide6 application entry point that sets up:
- QApplication with system tray
- WebSocket connection to Cortex daemon
- Dashboard window, overlay window, and settings dialog
- Signal routing between WebSocket events and UI components

Usage:
    python -m cortex.apps.desktop_shell.main
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import subprocess
import sys
import threading
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal, Slot
from PySide6.QtWidgets import QApplication

from cortex.apps.desktop_shell import mac_native
from cortex.apps.desktop_shell.dashboard import DashboardWindow
from cortex.apps.desktop_shell.onboarding import OnboardingWindow, onboarding_marker_path
from cortex.apps.desktop_shell.overlay import OverlayWindow
from cortex.apps.desktop_shell.settings import SettingsDialog
from cortex.apps.desktop_shell.tray import CortexTrayIcon
from cortex.libs.config.settings import APIConfig, get_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# WebSocket bridge: runs asyncio in a background thread, emits Qt signals
# ---------------------------------------------------------------------------

class WebSocketBridge(QObject):
    """Bridges async WebSocket events to Qt signals."""

    state_updated = Signal(dict)
    intervention_triggered = Signal(dict)
    intervention_restored = Signal(dict)
    settings_synced = Signal(dict)
    connection_changed = Signal(bool)

    def __init__(self, host: str = "127.0.0.1", port: int = 9473) -> None:
        super().__init__()
        self._host = host
        self._port = port
        self._running = False
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws: Any = None
        # E.6: exponential reconnect backoff (3 → 6 → 12 → 24 → 30s cap),
        # matching the browser and VS Code clients. Resets to 3s after a
        # successful connect.
        self._reconnect_delay = 3.0
        self._reconnect_delay_max = 30.0

    def start(self) -> None:
        """Start the WebSocket listener in a background thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the WebSocket listener."""
        self._running = False
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    def send_user_action(self, action: str, intervention_id: str) -> None:
        """Send a USER_ACTION message to the daemon."""
        if self._loop is None or self._ws is None:
            return
        msg = json.dumps({
            "type": "USER_ACTION",
            "payload": {"action": action, "intervention_id": intervention_id},
            "timestamp": 0,
            "sequence": 0,
        })
        asyncio.run_coroutine_threadsafe(self._send(msg), self._loop)

    def send_shutdown(self) -> None:
        """E.1 (WS-mode Stop button): send a top-level SHUTDOWN message.

        The WS server handles ``type=SHUTDOWN`` directly (it routes to
        ``_request_shutdown`` → SIGTERM). The previous implementation went
        through USER_ACTION, which the daemon dropped because no
        ``intervention_id`` was attached.
        """
        if self._loop is None or self._ws is None:
            return
        msg = json.dumps({
            "type": "SHUTDOWN",
            "payload": {},
            "timestamp": 0,
            "sequence": 0,
        })
        asyncio.run_coroutine_threadsafe(self._send(msg), self._loop)

    def send_settings(self, settings: dict[str, Any]) -> None:
        """Send SETTINGS_SYNC to the daemon."""
        if self._loop is None or self._ws is None:
            return
        msg = json.dumps({
            "type": "SETTINGS_SYNC",
            "payload": settings,
            "timestamp": 0,
            "sequence": 0,
        })
        asyncio.run_coroutine_threadsafe(self._send(msg), self._loop)

    async def _send(self, msg: str) -> None:
        """Send a message over the WebSocket."""
        if self._ws is not None:
            try:
                await self._ws.send(msg)
            except Exception:
                pass

    def _run_loop(self) -> None:
        """Run the asyncio event loop in a background thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._connect_loop())

    async def _connect_loop(self) -> None:
        """Connect to WebSocket with auto-reconnect.

        swift-concurrency-pro gap fix: previously a bare ``except Exception``
        swallowed :class:`asyncio.CancelledError`, so :meth:`stop` could not
        unwind the loop cleanly. We now re-raise ``CancelledError`` (the
        Swift-concurrency "let cancellation propagate" rule in Python idiom)
        and only catch the network-shaped exceptions for the reconnect path.
        """
        while self._running:
            try:
                import websockets

                uri = f"ws://{self._host}:{self._port}"
                async with websockets.connect(uri) as ws:
                    self._ws = ws
                    self.connection_changed.emit(True)
                    logger.info(f"Connected to Cortex daemon at {uri}")
                    # E.6: successful connect → reset backoff.
                    self._reconnect_delay = 3.0

                    # Identify as desktop client
                    identify_msg = json.dumps({
                        "type": "IDENTIFY",
                        "payload": {"client_type": "desktop"},
                        "timestamp": 0,
                        "sequence": 0,
                    })
                    await ws.send(identify_msg)

                    async for raw in ws:
                        if not self._running:
                            break
                        self._handle_message(raw)

            except ImportError:
                logger.error("websockets package not installed")
                break
            except asyncio.CancelledError:
                # Propagate cleanly so the event loop can shut down.
                self._ws = None
                self.connection_changed.emit(False)
                raise
            except (OSError, ConnectionError) as e:
                self._ws = None
                self.connection_changed.emit(False)
                logger.debug(f"WebSocket disconnected: {e}")
                if self._running:
                    await asyncio.sleep(self._reconnect_delay)
                    self._reconnect_delay = min(
                        self._reconnect_delay * 2.0, self._reconnect_delay_max,
                    )
            except Exception as e:
                # ``websockets.ConnectionClosed`` and similar transient
                # protocol errors don't subclass OSError. Keep them in
                # the reconnect path but distinguish them from
                # cancellation, which is handled above.
                self._ws = None
                self.connection_changed.emit(False)
                logger.debug(f"WebSocket disconnected: {e}")
                if self._running:
                    await asyncio.sleep(self._reconnect_delay)
                    self._reconnect_delay = min(
                        self._reconnect_delay * 2.0, self._reconnect_delay_max,
                    )

    def _handle_message(self, raw: str) -> None:
        """Parse and dispatch a WebSocket message."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = msg.get("type", "")
        payload = msg.get("payload", {})

        if msg_type == "STATE_UPDATE":
            self.state_updated.emit(payload)
        elif msg_type == "INTERVENTION_TRIGGER":
            self.intervention_triggered.emit(payload)
        elif msg_type == "INTERVENTION_RESTORE":
            self.intervention_restored.emit(payload)
        elif msg_type == "SETTINGS_SYNC":
            self.settings_synced.emit(payload)


# ---------------------------------------------------------------------------
# Main application controller
# ---------------------------------------------------------------------------

class CortexApp:
    """
    Main desktop shell application.

    Orchestrates the tray icon, dashboard, overlay, settings dialog,
    and WebSocket connection to the Cortex daemon.
    """

    def __init__(self, config: APIConfig | None = None) -> None:
        self._config = config or get_config().api
        self._app: QApplication | None = None
        self._tray: CortexTrayIcon | None = None
        self._dashboard: DashboardWindow | None = None
        self._overlay: OverlayWindow | None = None
        self._settings: SettingsDialog | None = None
        self._onboarding: OnboardingWindow | None = None
        self._bridge: WebSocketBridge | None = None
        self._paused = False
        self._active_intervention_id: str | None = None

    def run(self) -> int:
        """Run the application. Returns exit code."""
        self._app = QApplication(sys.argv)
        self._app.setApplicationName("Cortex")
        self._app.setOrganizationName("Cortex")
        self._app.setQuitOnLastWindowClosed(False)  # Keep running in tray

        # Create UI components
        self._dashboard = DashboardWindow()
        self._overlay = OverlayWindow()
        self._settings = SettingsDialog()
        self._onboarding = OnboardingWindow()

        # Create tray icon
        self._tray = CortexTrayIcon(self._app)
        self._tray.show_dashboard_requested.connect(self._show_dashboard)
        self._tray.show_settings_requested.connect(self._show_settings)
        # E.4: wire the Connect Extensions menu entry in WS mode too —
        # previously only the in-process CortexAppController hooked it,
        # so the menu item silently did nothing under --ws mode.
        if hasattr(self._tray, "show_connections_requested"):
            self._tray.show_connections_requested.connect(
                self._show_connections,
            )
        self._tray.pause_requested.connect(self._toggle_pause)
        self._tray.restore_requested.connect(self._restore_workspace)
        self._tray.snooze_requested.connect(self._snooze_fifteen_minutes)
        self._tray.disable_session_requested.connect(self._disable_for_session)
        self._tray.quit_requested.connect(self._quit)

        # E.1: route dashboard Stop / goal signals to the daemon. Stop
        # tears down via WebSocket SHUTDOWN; goal_set posts a USER_ACTION
        # that the daemon resolves to a current goal hint.
        if self._dashboard is not None:
            self._dashboard.stop_requested.connect(self._request_remote_shutdown)
            self._dashboard.goal_set.connect(self._send_goal)

        # Create WebSocket bridge
        self._bridge = WebSocketBridge(
            host=self._config.host,
            port=self._config.ws_port,
        )
        self._bridge.state_updated.connect(self._on_state_update)
        self._bridge.intervention_triggered.connect(self._on_intervention)
        self._bridge.intervention_restored.connect(self._on_restore)
        self._bridge.settings_synced.connect(self._on_settings_synced)
        self._bridge.connection_changed.connect(self._on_connection_changed)

        # Connect overlay dismiss to user action
        self._overlay.dismissed.connect(self._on_overlay_dismissed)

        # Connect settings changes
        self._settings.settings_changed.connect(self._on_settings_changed)
        self._onboarding.open_settings_requested.connect(self._show_settings)
        self._onboarding.run_calibration_requested.connect(self._run_calibration)
        self._onboarding.completed.connect(self._complete_onboarding)
        # E.5: step-4 "Open Connections" button.
        if hasattr(self._onboarding, "extensions_requested"):
            self._onboarding.extensions_requested.connect(self._show_connections)

        # Start WebSocket connection
        self._bridge.start()

        # Handle SIGINT gracefully
        signal.signal(signal.SIGINT, lambda *_: self._quit())
        # Timer to allow Python signal handling
        timer = QTimer()
        timer.timeout.connect(lambda: None)
        timer.start(500)

        # Show tray icon
        self._tray.show()
        try:
            self._tray.install_native_status_item()
        except Exception:
            logger.debug("native status item install failed", exc_info=True)

        # Apply native window chrome to top-level windows. Each window's
        # ``showEvent`` also calls these to keep things robust on re-show.
        try:
            for window in (self._dashboard, self._settings, self._overlay,
                           self._onboarding):
                if window is None:
                    continue
                mac_native.apply_unified_titlebar(window)
        except Exception:
            logger.debug("native chrome init failed", exc_info=True)

        if not onboarding_marker_path().exists():
            self._onboarding.show()

        return self._app.exec()

    @Slot(dict)
    def _on_state_update(self, payload: dict) -> None:
        """Handle STATE_UPDATE from daemon."""
        if self._paused:
            return
        if self._dashboard is not None:
            self._dashboard.update_state(payload)
        if self._tray is not None:
            state = payload.get("state", "FLOW")
            confidence = payload.get("confidence", 0.0)
            self._tray.update_state(state, confidence)

    @Slot(dict)
    def _on_intervention(self, payload: dict) -> None:
        """Handle INTERVENTION_TRIGGER from daemon."""
        if self._paused:
            return
        self._active_intervention_id = payload.get("intervention_id")
        if self._overlay is not None:
            self._overlay.show_intervention(payload)

    @Slot(bool)
    def _on_connection_changed(self, connected: bool) -> None:
        """Handle WebSocket connection state change."""
        if self._tray is not None:
            self._tray.set_connected(connected)
        if self._dashboard is not None:
            self._dashboard.set_connected(connected)

    @Slot(str)
    def _on_overlay_dismissed(self, intervention_id: str) -> None:
        """Handle overlay dismiss by user."""
        if self._bridge is not None:
            self._bridge.send_user_action("dismissed", intervention_id)

    @Slot(dict)
    def _on_settings_changed(self, settings: dict) -> None:
        """Handle settings changes."""
        if self._bridge is not None:
            self._bridge.send_settings(settings)
        logger.info(f"Settings updated: {settings}")

    @Slot(dict)
    def _on_restore(self, payload: dict) -> None:
        """Handle explicit restore events from the daemon."""
        self._active_intervention_id = None
        if self._overlay is not None:
            self._overlay.hide()

    @Slot(dict)
    def _on_settings_synced(self, payload: dict) -> None:
        """Handle settings sync from the daemon.

        E.2: round-trip daemon-side settings into the dialog widgets so
        the dashboard always shows the authoritative state. Previously
        the payload was logged and dropped, so any daemon-side mutation
        (quiet mode, intervention disable, etc.) never reached the UI.
        """
        logger.info(f"Settings synced: {payload}")
        if self._settings is not None:
            try:
                self._settings.apply_payload(payload)
            except Exception:
                logger.debug("Failed to apply settings payload", exc_info=True)

    def _show_dashboard(self) -> None:
        """Show the dashboard window."""
        if self._dashboard is not None:
            self._dashboard.show()
            self._dashboard.raise_()
            self._dashboard.activateWindow()

    def _show_settings(self) -> None:
        """Show the settings dialog."""
        if self._settings is not None:
            self._settings.show()
            self._settings.raise_()
            self._settings.activateWindow()

    def _show_connections(self) -> None:
        """Show the Connect Extensions panel (E.4).

        Lazy-imports so non-macOS / test harnesses without PySide6 dialogs
        don't pay the cost on startup.
        """
        try:
            from cortex.apps.desktop_shell.connections import ConnectionsPanel

            panel = getattr(self, "_connections_panel", None)
            if panel is None:
                panel = ConnectionsPanel()
                self._connections_panel = panel
            panel.show()
            panel.raise_()
            panel.activateWindow()
        except Exception:
            logger.warning("Connections panel unavailable", exc_info=True)

    def _request_remote_shutdown(self) -> None:
        """E.1 / E.4: tear down the daemon when the dashboard Stop button fires.

        Sends a top-level ``SHUTDOWN`` WS message (which the daemon's
        websocket_server handles via ``_request_shutdown`` → SIGTERM) and
        then quits the shell after the daemon has time to release the
        camera + WebSocket. The previous implementation used
        ``send_user_action("shutdown", "")`` which the daemon silently
        dropped because the action handler requires an intervention_id.
        """
        if self._bridge is not None:
            try:
                self._bridge.send_shutdown()
            except Exception:
                pass
        # Give the daemon ~1s to receive and act on SHUTDOWN before the
        # shell exits (WS close races with the daemon's flush logic).
        QTimer.singleShot(1000, self._quit)

    def _send_goal(self, goal: str) -> None:
        """Forward the dashboard goal-input text to the daemon."""
        if self._bridge is None or not goal:
            return
        try:
            self._bridge.send_user_action(f"set_goal:{goal}", "")
        except Exception:
            pass

    def _run_calibration(self) -> None:
        """Kick off calibration in a detached subprocess."""
        try:
            subprocess.Popen([sys.executable, "-m", "cortex.scripts.calibrate"])
        except OSError as exc:
            logger.error("Failed to launch calibration: %s", exc)

    def _complete_onboarding(self) -> None:
        """Mark onboarding complete for future launches."""
        marker = onboarding_marker_path()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("completed\n")
        if self._onboarding is not None:
            self._onboarding.hide()

    def _toggle_pause(self) -> None:
        """Toggle pause/resume state."""
        self._paused = not self._paused
        if self._tray is not None:
            self._tray.set_paused(self._paused)
        if self._paused and self._overlay is not None:
            self._overlay.hide()
        logger.info(f"Cortex {'paused' if self._paused else 'resumed'}")

    def _restore_workspace(self) -> None:
        """Restore the current intervention if one is active."""
        if self._bridge is not None and self._active_intervention_id:
            self._bridge.send_user_action("dismissed", self._active_intervention_id)

    def _snooze_fifteen_minutes(self) -> None:
        """Enable 15-minute quiet mode."""
        if self._bridge is not None:
            self._bridge.send_settings({
                "quiet_mode": True,
                "quiet_duration_minutes": 15,
            })

    def _disable_for_session(self) -> None:
        """Turn off auto-interventions for the rest of the session."""
        if self._bridge is not None:
            self._bridge.send_settings({
                "interventions_enabled": False,
            })
        self._paused = True
        if self._tray is not None:
            self._tray.set_paused(True)
        if self._overlay is not None:
            self._overlay.hide()

    def _quit(self) -> None:
        """Quit the application."""
        logger.info("Shutting down Cortex desktop shell")
        if self._bridge is not None:
            self._bridge.stop()
        if self._overlay is not None:
            self._overlay.close()
        if self._dashboard is not None:
            self._dashboard.close()
        if self._app is not None:
            self._app.quit()


def main() -> None:
    """Entry point for the desktop shell.

    In bundled mode (``sys.frozen``), boots the daemon in-process via
    :class:`CortexAppController`.  In dev mode, falls back to the
    WebSocket-based :class:`CortexApp` unless ``--in-process`` is passed.
    """
    logging.basicConfig(level=logging.INFO)

    use_in_process = getattr(sys, "frozen", False) or "--in-process" in sys.argv

    if use_in_process:
        from cortex.apps.desktop_shell.controller import CortexAppController

        controller = CortexAppController()
        sys.exit(controller.run())
    else:
        app = CortexApp()
        sys.exit(app.run())


if __name__ == "__main__":
    main()
