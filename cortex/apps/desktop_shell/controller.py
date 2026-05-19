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

from cortex.apps.desktop_shell import mac_native
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
    # F34: emitted on the Qt main thread when the daemon's ``stop()`` future
    # resolves (or the safety timer fires). UI surfaces (dashboard, tray)
    # listen so they can re-enable their Stop affordances.
    daemon_stopped = Signal()
    # Phase J-2: surface daemon errors to the dashboard top-bar toast with
    # a correlation id quoted back to the user. Payload is (title, body,
    # cid) — strings rather than a dict so Qt's queued-connection
    # marshalling is dirt-cheap and the contract is easy to grep.
    error_occurred = Signal(str, str, str)

    # -- callbacks invoked from daemon thread ---------------------------------
    #
    # F17 (audit): per-type monotonic ``_seq`` drop. The daemon stamps a
    # ``_seq`` field on every payload it hands these callbacks; we
    # remember the last applied value per channel and silently drop
    # anything that is not strictly greater. This protects the UI from
    # reordered or duplicated frames on the daemon→bridge edge (in-
    # process there is no real reorder risk, but the same drop-stale
    # invariant lets the bridge be safely shared with future
    # cross-process callbacks).
    _LAST_STATE_SEQ_DEFAULT: int = 0
    _LAST_INTERVENTION_SEQ_DEFAULT: int = 0

    def __init__(self) -> None:  # type: ignore[override]
        super().__init__()
        self._last_state_seq: int = self._LAST_STATE_SEQ_DEFAULT
        self._last_intervention_seq: int = self._LAST_INTERVENTION_SEQ_DEFAULT

    def on_state(self, payload: dict) -> None:
        """State callback — payload is already deep-copied by the daemon.

        F17: drops the frame if ``payload['_seq']`` is not strictly
        greater than the last applied value. Frames without ``_seq``
        (older daemon builds, test fixtures) bypass the check.
        """
        seq = payload.get("_seq")
        if isinstance(seq, int):
            if seq <= self._last_state_seq:
                logger.debug(
                    "F17: dropping stale STATE frame seq=%s last=%s",
                    seq, self._last_state_seq,
                )
                return
            self._last_state_seq = seq
        self.state_updated.emit(payload)

    def on_intervention(self, payload: dict) -> None:
        """Intervention callback — payload is already deep-copied.

        F17: same drop-stale guard as ``on_state``. The intervention
        channel benefits even more from sequencing: a reordered trigger
        could overwrite an active intervention with a stale plan.
        """
        seq = payload.get("_seq")
        if isinstance(seq, int):
            if seq <= self._last_intervention_seq:
                logger.debug(
                    "F17: dropping stale INTERVENTION frame seq=%s last=%s",
                    seq, self._last_intervention_seq,
                )
                return
            self._last_intervention_seq = seq
        self.intervention_triggered.emit(payload)

    def reset_sequence_counters(self) -> None:
        """F17: reset both sequence counters. Called when the underlying
        daemon restarts (in-process re-init, daemon stop+start) so the
        next first-frame from the fresh daemon is not rejected as
        stale against the previous-incarnation counter."""
        self._last_state_seq = self._LAST_STATE_SEQ_DEFAULT
        self._last_intervention_seq = self._LAST_INTERVENTION_SEQ_DEFAULT

    def on_daemon_stopped(self) -> None:
        """Called when the in-process daemon's ``stop()`` future resolves."""
        self.daemon_stopped.emit()

    def on_error(self, title: str, body: str, cid: str = "") -> None:
        """Phase J-2: surface a daemon error in the dashboard toast.

        Daemon-thread callers reach into the F19 correlation context to
        pull the cid; if none is bound they pass the empty string and the
        toast simply renders ``ref:`` with no value. Callers do not need
        to wrap title/body — the toast clips overflowing text via the
        host QLabel's word-wrap.
        """
        # Defensive: title is the only mandatory field. Body + cid can be
        # empty so the controller can surface a "Cortex offline" toast
        # before any cid has been minted (e.g. WS handshake failure).
        self.error_occurred.emit(str(title or "Error"), str(body or ""), str(cid or ""))


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
        # F34: re-enable dashboard + tray Stop affordances when the daemon
        # actually reports stopped (or the dashboard/tray's own safety-timer
        # fires).
        self._bridge.daemon_stopped.connect(self._on_daemon_stopped)
        # Phase J-2: route daemon errors into the dashboard top-bar toast.
        self._bridge.error_occurred.connect(self._on_error_occurred)

        self._overlay.dismissed.connect(self._on_overlay_dismissed)
        self._settings.settings_changed.connect(self._on_settings_changed)
        self._settings.back_requested.connect(self._show_dashboard)
        self._connections.back_requested.connect(self._show_dashboard)
        self._onboarding.open_settings_requested.connect(self._show_settings)
        self._onboarding.run_calibration_requested.connect(self._run_calibration)
        self._onboarding.completed.connect(self._complete_onboarding)
        # E.5 (DMG-path completion): step-4 "Open Connections" button.
        # Previously only the WS-mode CortexApp wired this signal, so the
        # DMG-shipping in-process controller left the button dead.
        if hasattr(self._onboarding, "extensions_requested"):
            self._onboarding.extensions_requested.connect(self._show_connections)
        # Audit-2 fix: hot-reload the LLM planner credentials when the
        # user saves a BYOK token in onboarding so the first session
        # actually uses the real LLM instead of the rule-based fallback.
        if hasattr(self._onboarding, "byok_token_saved"):
            self._onboarding.byok_token_saved.connect(self._reload_llm_credentials)
        # E.1 (DMG-path completion): dashboard Stop button and goal input.
        # The bundled controller never owns a WebSocket — its `stop` path is
        # to schedule the in-process daemon's `stop()` coroutine on the
        # daemon thread loop, then quit the Qt app.
        if hasattr(self._dashboard, "stop_requested"):
            self._dashboard.stop_requested.connect(self._stop_daemon_and_quit)
        if hasattr(self._dashboard, "goal_set"):
            self._dashboard.goal_set.connect(self._on_goal_set)

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
        # Best-effort NSStatusItem upgrade: replaces the Qt-rendered tray
        # icon with a templated SF Symbol heart that follows menu-bar
        # appearance (light/dark) automatically. No-op on non-mac.
        try:
            self._tray.install_native_status_item()
        except Exception:
            logger.debug("native status item install failed", exc_info=True)

        # Force the app to activate as a foreground app on macOS.
        # PyInstaller bundles don't always get proper activation, so the
        # dashboard window can be created but hidden behind other windows.
        try:
            from AppKit import (  # type: ignore[import-untyped]
                NSApp,
                NSApplicationActivationPolicyRegular,
            )

            NSApp.setActivationPolicy_(NSApplicationActivationPolicyRegular)
            NSApp.activateIgnoringOtherApps_(True)
        except ImportError:
            pass  # Not on macOS or pyobjc not available

        # Defer dashboard show to after the event loop starts so that
        # NSApp activation actually takes effect.
        def _initial_show() -> None:
            logger.info("_initial_show: showing dashboard window")
            try:
                self._show_dashboard()
                # Each window's ``showEvent`` now applies vibrancy + unified
                # titlebar internally via mac_native; this hook is the
                # belt-and-suspenders re-application for the dashboard,
                # which can be reshown without going through __init__.
                if self._dashboard is not None:
                    mac_native.apply_unified_titlebar(self._dashboard)
                    mac_native.apply_vibrancy(
                        self._dashboard, material="window_background",
                    )
                logger.info("_initial_show: dashboard shown successfully, visible=%s",
                            self._dashboard.isVisible() if self._dashboard else "None")
            except Exception:
                logger.exception("_initial_show: failed to show dashboard")
            # Show onboarding on top if first launch
            if not onboarding_marker_path().exists():
                self._onboarding.show()
                self._onboarding.raise_()
                self._onboarding.activateWindow()

        QTimer.singleShot(200, _initial_show)

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
            finally:
                # F34: notify the UI that the stop attempt resolved (either
                # successfully or by timeout) so the Stop button re-enables.
                try:
                    self._bridge.on_daemon_stopped()
                except Exception:
                    logger.debug(
                        "daemon_stopped emit failed (non-fatal)", exc_info=True
                    )

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
        # Audit-2 fix: bump the Today/Blocked counter so the dashboard
        # numeric reflects reality instead of staying at the placeholder.
        if self._dashboard is not None and hasattr(
            self._dashboard, "record_intervention_seen"
        ):
            try:
                self._dashboard.record_intervention_seen()
            except Exception:
                logger.debug("record_intervention_seen failed", exc_info=True)

    @Slot(bool)
    def _on_connection_changed(self, connected: bool) -> None:
        if self._tray is not None:
            self._tray.set_connected(connected)
        if self._dashboard is not None:
            self._dashboard.set_connected(connected)

    @Slot()
    def _on_daemon_stopped(self) -> None:
        """F34: the daemon's stop() resolved on the main thread; re-enable
        the dashboard Stop button and the tray Quit action."""
        if self._dashboard is not None and hasattr(
            self._dashboard, "notify_daemon_stopped"
        ):
            self._dashboard.notify_daemon_stopped()
        if self._tray is not None and hasattr(
            self._tray, "notify_daemon_stopped"
        ):
            self._tray.notify_daemon_stopped()

    @Slot(str, str, str)
    def _on_error_occurred(self, title: str, body: str, cid: str) -> None:
        """Phase J-2: forward bridge error events to the dashboard's
        top-bar toast. Defensive: the dashboard may not yet exist on
        early-startup errors; we drop the toast in that case (the daemon's
        own structured-log already carries the cid)."""
        if self._dashboard is None or not hasattr(self._dashboard, "show_error"):
            logger.warning(
                "Dashboard unavailable for error toast: %s — %s [cid=%s]",
                title, body, cid,
            )
            return
        try:
            self._dashboard.show_error(title, body, cid)
        except Exception:
            logger.debug("Toast surface failed", exc_info=True)

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
            # Force macOS to bring the app + window to front
            try:
                from AppKit import NSApp  # type: ignore[import-untyped]

                NSApp.activateIgnoringOtherApps_(True)
                # Raise the key window directly
                if NSApp.keyWindow():
                    NSApp.keyWindow().makeKeyAndOrderFront_(None)
                elif NSApp.mainWindow():
                    NSApp.mainWindow().makeKeyAndOrderFront_(None)
            except Exception:
                pass

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

    def _stop_daemon_and_quit(self) -> None:
        """E.1 (DMG path): tear down the in-process daemon then quit Qt.

        The dashboard Stop button used to do nothing in DMG mode. Now it
        schedules ``daemon.stop()`` on the daemon thread's event loop —
        which releases the camera, finalises the SessionReport, and closes
        the WebSocket — before quitting the Qt app via ``_quit`` (which
        also triggers ``aboutToQuit → _shutdown_daemon`` as a safety net).
        """
        logger.info("Dashboard Stop button — stopping daemon and quitting")
        if self._daemon is not None and self._daemon_loop is not None and self._daemon_loop.is_running():
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._daemon.stop(), self._daemon_loop,
                )
                # Wait a short time so the camera is released before Qt exits.
                future.result(timeout=5.0)
            except Exception:
                logger.warning("Daemon stop timed out from dashboard Stop", exc_info=True)
            finally:
                # F34: signal the UI that stop resolved (success or timeout).
                try:
                    self._bridge.on_daemon_stopped()
                except Exception:
                    logger.debug(
                        "daemon_stopped emit failed (non-fatal)", exc_info=True
                    )
        self._quit()

    def _on_goal_set(self, goal: str) -> None:
        """E.1 (DMG path): forward dashboard goal input to the daemon.

        The in-process controller can mutate the daemon directly — no
        round-trip through WebSocket needed. ``set_user_goal`` updates
        the daemon's ``_user_goal_override`` so the next ``_context_loop``
        tick injects it into ``context.current_goal_hint``.
        """
        cleaned = (goal or "").strip()
        if not cleaned or self._daemon is None or self._daemon_loop is None:
            return
        try:
            # Audit-2 fix: ``set_user_goal`` is the real daemon API.
            # Previously called ``set_current_goal`` which never existed
            # — silently fell through the ``hasattr`` guard.
            if hasattr(self._daemon, "set_user_goal"):
                asyncio.run_coroutine_threadsafe(
                    self._daemon.set_user_goal(cleaned),
                    self._daemon_loop,
                )
            elif hasattr(self._daemon, "set_current_goal"):
                asyncio.run_coroutine_threadsafe(
                    self._daemon.set_current_goal(cleaned),
                    self._daemon_loop,
                )
        except Exception:
            logger.debug("Failed to forward goal to daemon", exc_info=True)

    def _reload_llm_credentials(self) -> None:
        """Audit-2 fix: hot-reload the planner's SDK client after BYOK save.

        Runs on the daemon thread so the SDK rebuild does not race with
        an in-flight ``generate_intervention_plan`` call.
        """
        if self._daemon is None or self._daemon_loop is None:
            return
        planner = getattr(self._daemon, "_llm_client", None)
        if planner is None or not hasattr(planner, "reload_credentials"):
            return

        def _do_reload() -> None:
            try:
                ok = planner.reload_credentials()
                if ok:
                    logger.info("LLM planner credentials reloaded after BYOK save")
                else:
                    logger.warning("LLM planner credentials reload returned False")
            except Exception:
                logger.exception("LLM planner credentials reload raised")

        try:
            self._daemon_loop.call_soon_threadsafe(_do_reload)
        except Exception:
            logger.debug("Failed to schedule reload_credentials", exc_info=True)
