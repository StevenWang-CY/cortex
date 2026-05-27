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
from cortex.libs.auth import load_or_create_token
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
    # P0 §3.1 / §3.2 / §3.3: history / trends / recap inbound payloads.
    session_list_received = Signal(dict)
    session_detail_received = Signal(dict)
    trends_received = Signal(dict)
    session_recap_received = Signal(dict)
    # P0 §3.4: locally-emitted calibration progress (not received over
    # the WebSocket — the runner is in-process). Lives here so the
    # CortexApp's wiring sites have a single thread-safe queueing point.
    calibration_progress = Signal(dict)

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
        # F17 (audit): per-message-type last-applied envelope sequence.
        # The daemon's WS server increments ``WSMessage.sequence`` once
        # per outbound message; receivers drop any frame whose sequence
        # is not strictly greater than the last applied value for its
        # type. Reset to {} on every fresh connect so a daemon restart
        # always wins.
        self._last_seq_by_type: dict[str, int] = {}
        self._reconnect_delay_max = 30.0
        # Debt-2 (audit): cache the capability token at startup so we can
        # AUTH on every (re)connect without re-reading the file. The
        # token rotates via Settings → "Rotate authentication token";
        # that path calls :meth:`refresh_auth_token` so the cache here
        # stays in sync.
        try:
            self._auth_token: str | None = load_or_create_token()
        except Exception:
            logger.exception("Could not load capability token; AUTH will fail")
            self._auth_token = None

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

    def send_action_execute(
        self,
        intervention_id: str,
        action: dict[str, Any],
        *,
        request_dispatch: bool,
    ) -> None:
        """G4 (audit-prod): send ACTION_EXECUTE from the desktop overlay.

        ``request_dispatch=True`` tells the daemon to additionally
        forward as ACTION_DISPATCH to chrome/edge so the action actually
        runs in the browser. For native-executed actions, set False —
        the message becomes a pure recorder log.
        """
        if self._loop is None or self._ws is None:
            return
        payload: dict[str, Any] = {
            "intervention_id": intervention_id,
            "action_id": action.get("action_id"),
            "action_type": action.get("action_type"),
            "label": action.get("label"),
            "reason": action.get("reason"),
            "target": action.get("target"),
            "tab_index": action.get("tab_index"),
            "action": action,
            "request_dispatch": bool(request_dispatch),
        }
        msg = json.dumps({
            "type": "ACTION_EXECUTE",
            "payload": payload,
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

        Audit-2 fix: stamp the capability token into ``payload.auth_token``
        so the F07 SHUTDOWN gate accepts the message. The Debt-2 AUTH
        handshake authenticates the connection; SHUTDOWN is double-gated
        (defense in depth) and the daemon rejects payloads without the
        token even on an already-authenticated socket.
        """
        if self._loop is None or self._ws is None:
            return
        payload: dict[str, Any] = {}
        if self._auth_token:
            payload["auth_token"] = self._auth_token
        msg = json.dumps({
            "type": "SHUTDOWN",
            "payload": payload,
            "timestamp": 0,
            "sequence": 0,
        })
        asyncio.run_coroutine_threadsafe(self._send(msg), self._loop)

    def send_request_session_list(self, since: float | None, limit: int) -> None:
        """P0 §3.1: ask the daemon for a paginated session listing."""
        if self._loop is None or self._ws is None:
            return
        payload: dict[str, Any] = {
            "since": float(since) if since is not None else None,
            "limit": int(limit) if limit else 30,
        }
        msg = json.dumps({
            "type": "REQUEST_SESSION_LIST",
            "payload": payload,
            "timestamp": 0,
            "sequence": 0,
        })
        asyncio.run_coroutine_threadsafe(self._send(msg), self._loop)

    def send_request_session_detail(self, session_id: str) -> None:
        """P0 §3.1: ask the daemon for a single ``SessionReport``."""
        if self._loop is None or self._ws is None or not session_id:
            return
        msg = json.dumps({
            "type": "REQUEST_SESSION_DETAIL",
            "payload": {"session_id": str(session_id)},
            "timestamp": 0,
            "sequence": 0,
        })
        asyncio.run_coroutine_threadsafe(self._send(msg), self._loop)

    def send_request_trends(self, window: str, refresh: bool) -> None:
        """P0 §3.2: ask the daemon for a longitudinal rollup.

        Phase 4.B fix (#18): drop the "quarter" alternative — the
        schema is ``Literal["week", "month"]``. Anything unexpected
        degrades to "week" so the request still completes."""
        if self._loop is None or self._ws is None:
            return
        win = window if window in ("week", "month") else "week"
        msg = json.dumps({
            "type": "REQUEST_TRENDS",
            "payload": {"window": win, "refresh": bool(refresh)},
            "timestamp": 0,
            "sequence": 0,
        })
        asyncio.run_coroutine_threadsafe(self._send(msg), self._loop)

    def send_request_session_recap(self) -> None:
        """P0 §3.3: ask the daemon for the most-recent cached recap.

        Used by surfaces (e.g. browser popup) that joined after the live
        broadcast was emitted; the desktop dashboard normally receives
        the live broadcast directly, so this is here for symmetry."""
        if self._loop is None or self._ws is None:
            return
        msg = json.dumps({
            "type": "REQUEST_SESSION_RECAP",
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

    def refresh_auth_token(self) -> str | None:
        """Re-read the capability token from disk and force a reconnect
        so the new value is sent on the AUTH handshake. Audit Debt-2:
        called by the Settings panel's "Rotate authentication token"
        button. Returns the new token (or None on read failure)."""
        try:
            self._auth_token = load_or_create_token()
        except Exception:
            logger.exception("Failed to refresh capability token")
            self._auth_token = None
            return None
        # Drop the active socket so the connect loop reconnects with the
        # new token in the AUTH frame. ``_running`` stays True so the
        # loop iterates again.
        ws = self._ws
        loop = self._loop
        if ws is not None and loop is not None:
            async def _close_active_ws() -> None:
                try:
                    await ws.close()
                except Exception:
                    logger.debug("close on active WS failed", exc_info=True)
            asyncio.run_coroutine_threadsafe(_close_active_ws(), loop)
        return self._auth_token

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
                    # F17 (audit): clear the per-type drop-stale tracker
                    # on every connect. A daemon restart resets its
                    # WSMessage.sequence counter to 0; without clearing
                    # here the receiver would reject every post-restart
                    # frame as "stale" until the new daemon's counter
                    # caught up with the pre-restart value.
                    self._last_seq_by_type.clear()

                    # Debt-2 (audit): AUTH is the contractual first
                    # frame. The daemon refuses every other type until
                    # this message validates. We send the cached token
                    # synchronously inline rather than via :meth:`_send`
                    # so an unauthenticated socket cannot be tricked
                    # into emitting a state frame from another path.
                    if self._auth_token:
                        auth_msg = json.dumps({
                            "type": "AUTH",
                            "payload": {"auth_token": self._auth_token},
                            "timestamp": 0,
                            "sequence": 0,
                        })
                        await ws.send(auth_msg)
                    else:
                        logger.warning(
                            "No auth token available; daemon will close "
                            "this connection. Retry after token provisioning.",
                        )

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

        # F17 (audit): per-type drop-stale on the WSMessage envelope
        # ``sequence`` field. The daemon increments this once per
        # outbound message; receivers maintain a per-type last-applied
        # value and ignore any frame whose sequence isn't strictly
        # greater. ``sequence=0`` from older daemons or test fixtures
        # bypasses the check (the default goes through on the first
        # frame only, which is the safe behaviour at connect time).
        seq = msg.get("sequence", 0)
        if isinstance(seq, int) and seq > 0 and msg_type:
            last = self._last_seq_by_type.get(msg_type, 0)
            if seq <= last:
                logger.debug(
                    "F17: dropping stale %s frame seq=%d last=%d",
                    msg_type, seq, last,
                )
                return
            self._last_seq_by_type[msg_type] = seq

        if msg_type == "STATE_UPDATE":
            self.state_updated.emit(payload)
        elif msg_type == "INTERVENTION_TRIGGER":
            self.intervention_triggered.emit(payload)
        elif msg_type == "INTERVENTION_RESTORE":
            self.intervention_restored.emit(payload)
        elif msg_type == "SETTINGS_SYNC":
            self.settings_synced.emit(payload)
        # P0 §3.1 / §3.2 / §3.3: history / trends / recap inbound dispatch.
        elif msg_type == "SESSION_LIST":
            self.session_list_received.emit(payload if isinstance(payload, dict) else {})
        elif msg_type == "SESSION_DETAIL":
            self.session_detail_received.emit(payload if isinstance(payload, dict) else {})
        elif msg_type == "TRENDS_PAYLOAD":
            self.trends_received.emit(payload if isinstance(payload, dict) else {})
        elif msg_type == "SESSION_RECAP":
            # Phase 4.B fix (#30): empty payloads ARE meaningful — the
            # daemon broadcasts ``{}`` for short sessions to tell the
            # dashboard to finalise its stop flow without opening the
            # recap sheet. Forward an empty dict in that case; the
            # dashboard's ``apply_session_recap`` handles the empty
            # payload as the synthetic short-session signal.
            self.session_recap_received.emit(payload if isinstance(payload, dict) else {})


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
        # P0 §3.4: in-flight CalibrationRunner. None when idle.
        self._calibration_runner: Any = None
        # P0 §3.4: in-flight CalibrationRunner. None when idle.
        self._calibration_runner: Any = None
        # P0 §3.4: in-flight CalibrationRunner. None when idle.
        self._calibration_runner: Any = None

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
        # Phase 4.B fix (#4): tray Quit honours the two-phase recap when
        # a session is active. See ``_on_user_initiated_quit``.
        self._tray.quit_requested.connect(self._on_user_initiated_quit)

        # E.1 / Phase 4.B fix (#3): route dashboard Stop / goal signals
        # to the daemon. The two-signal split mirrors the in-process
        # controller (see ``controller.CortexAppController`` for the
        # rationale):
        #
        # * ``daemon_stop_requested`` — sends the SHUTDOWN WS frame so
        #   the daemon broadcasts SESSION_RECAP and then exits. Does
        #   NOT quit Qt.
        # * ``gui_quit_requested`` — quits Qt after the user has
        #   consumed the recap (or the watchdog fired).
        #
        # The legacy ``stop_requested`` is an alias for daemon-stop;
        # keeping it wired to the same slot maintains backward compat.
        if self._dashboard is not None:
            if hasattr(self._dashboard, "daemon_stop_requested"):
                self._dashboard.daemon_stop_requested.connect(
                    self._on_daemon_stop_requested,
                )
            if hasattr(self._dashboard, "gui_quit_requested"):
                self._dashboard.gui_quit_requested.connect(
                    self._on_gui_quit_requested,
                )
            self._dashboard.stop_requested.connect(self._on_daemon_stop_requested)
            self._dashboard.goal_set.connect(self._send_goal)
            # P0 §3.1 / §3.2: route History tab outgoing requests onto
            # the WS wire. The dashboard re-emits these from the embedded
            # HistoryTab; the bridge translates them to typed WS frames.
            if hasattr(self._dashboard, "history_requested"):
                self._dashboard.history_requested.connect(
                    self._on_history_requested,
                )
            if hasattr(self._dashboard, "detail_requested"):
                self._dashboard.detail_requested.connect(
                    self._on_detail_requested,
                )
            if hasattr(self._dashboard, "trends_requested"):
                self._dashboard.trends_requested.connect(
                    self._on_trends_requested,
                )
            # Audit-2 fix: wire the dashboard's Connect button in WS mode.
            # The DMG ``--in-process`` path already routes through
            # ``CortexAppController``; the WS path previously left this
            # control dead (clicking did nothing).
            try:
                consumer = getattr(self._dashboard, "_consumer", None)
                connect_btn = getattr(consumer, "_connect_btn", None) if consumer else None
                if connect_btn is not None:
                    connect_btn.clicked.connect(self._show_connections)
            except Exception:
                logger.debug("Failed to wire Connect button (non-fatal)", exc_info=True)

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
        # P0 §3.1 / §3.2 / §3.3: route inbound history / trends / recap
        # frames from the WS bridge straight to the dashboard. The
        # dashboard delegates to the HistoryTab and RecapSheet
        # internally.
        if self._dashboard is not None:
            self._bridge.session_list_received.connect(
                self._dashboard.apply_session_list,
            )
            self._bridge.session_detail_received.connect(
                self._dashboard.apply_session_detail,
            )
            self._bridge.trends_received.connect(
                self._dashboard.apply_trends,
            )
            self._bridge.session_recap_received.connect(
                self._dashboard.apply_session_recap,
            )

        # P0 §3.4: queue calibration progress onto the Qt main thread so
        # the onboarding card and dashboard freshness pill stay in sync
        # with the worker-thread runner.
        if self._bridge is not None and hasattr(self._bridge, "calibration_progress"):
            self._bridge.calibration_progress.connect(
                self._on_calibration_progress
            )

        # P0 §3.4: queue calibration progress onto the Qt main thread so
        # the onboarding card and dashboard freshness pill stay in sync
        # with the worker-thread runner.
        if self._bridge is not None and hasattr(self._bridge, "calibration_progress"):
            self._bridge.calibration_progress.connect(
                self._on_calibration_progress
            )

        # Connect overlay dismiss to user action
        self._overlay.dismissed.connect(self._on_overlay_dismissed)
        # G4 (audit-prod): overlay action buttons route here. Native
        # action types execute inline in the desktop shell; browser-bound
        # ones go to the daemon as an ACTION_EXECUTE with
        # ``request_dispatch=True`` so the daemon forwards as
        # ACTION_DISPATCH to the connected chrome / edge client.
        if hasattr(self._overlay, "action_invoked"):
            self._overlay.action_invoked.connect(self._on_action_invoked)

        # Connect settings changes
        self._settings.settings_changed.connect(self._on_settings_changed)
        # Debt-2 Commit 5: rotation drops the bridge's cached token,
        # forces a reconnect, and surfaces a confirmation toast.
        if hasattr(self._settings, "auth_token_rotated"):
            self._settings.auth_token_rotated.connect(
                lambda _tok: self._bridge.refresh_auth_token(),
            )
        self._onboarding.open_settings_requested.connect(self._show_settings)
        self._onboarding.run_calibration_requested.connect(self._run_calibration)
        self._onboarding.completed.connect(self._complete_onboarding)
        # P0 §3.4: Settings → Sensing → Recalibrate baselines also drives
        # the same in-process CalibrationRunner code path. The controller
        # owns the same wire-up (controller.py:415) — in the WS-mode
        # CortexApp we add it once here too, but the previously-duplicated
        # connect line was an audit regression that fired _run_calibration
        # twice per click.
        if hasattr(self._settings, "recalibrate_requested"):
            self._settings.recalibrate_requested.connect(self._run_calibration)
        # Audit-2 fix: ask the remote daemon to reload LLM credentials
        # after a BYOK save. Settings change with reload_llm_credentials=True
        # is the WS-protocol-friendly way to signal hot-reload.
        # B2 (audit-prod): on success we also surface a confirmation
        # toast on the dashboard.
        if hasattr(self._onboarding, "byok_token_saved"):
            self._onboarding.byok_token_saved.connect(self._on_byok_token_saved)
        # Audit-prod fix (P1-2): wire the previously-orphan
        # ``settings_save_failed`` Signal so the dashboard surfaces save
        # errors. ``auth_token_rotated`` is already wired below to
        # refresh the bridge.
        if hasattr(self._settings, "settings_save_failed"):
            self._settings.settings_save_failed.connect(
                self._on_settings_save_failed
            )
        # E.5: step-4 "Open Connections" button.
        if hasattr(self._onboarding, "extensions_requested"):
            self._onboarding.extensions_requested.connect(self._show_connections)

        # Start WebSocket connection
        self._bridge.start()

        # Phase 4.B fix (#4): route Cmd+W on the dashboard (which fires
        # ``lastWindowClosed`` with our ``setQuitOnLastWindowClosed(False)``
        # tray-resident config) through the two-phase recap when a
        # session is active.
        try:
            self._app.lastWindowClosed.connect(self._on_user_initiated_quit)
        except Exception:
            logger.debug("lastWindowClosed connect failed", exc_info=True)
        # Handle SIGINT gracefully — route via user-initiated quit so a
        # Ctrl+C from a dev terminal also gets the recap when applicable.
        signal.signal(signal.SIGINT, lambda *_: self._on_user_initiated_quit())
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
        # Audit-2 fix: bump the Today/Blocked counter so the dashboard
        # numeric reflects reality instead of staying at the "--" placeholder.
        if self._dashboard is not None and hasattr(
            self._dashboard, "record_intervention_seen"
        ):
            try:
                self._dashboard.record_intervention_seen()
            except Exception:
                logger.debug("record_intervention_seen failed", exc_info=True)

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

    def _on_settings_save_failed(self, reason: str) -> None:
        """Audit-prod fix (P1-2): surface failed save in the dashboard."""
        if self._dashboard is not None and hasattr(self._dashboard, "show_error"):
            try:
                self._dashboard.show_error(
                    "Settings save failed",
                    str(reason or "Unknown error — see daemon log."),
                    "",
                )
            except Exception:
                logger.debug("show_error failed", exc_info=True)

    def _on_byok_token_saved(self) -> None:
        """B2 (audit-prod): user saved a BYOK token; ask daemon to reload
        AND surface the success toast so the user knows the new token is
        live without having to restart.
        """
        if self._bridge is not None:
            try:
                self._bridge.send_settings({"reload_llm_credentials": True})
            except Exception:
                logger.debug("reload_llm_credentials send failed", exc_info=True)
        if self._dashboard is not None and hasattr(
            self._dashboard, "show_info_toast"
        ):
            try:
                self._dashboard.show_info_toast(
                    "Cortex is now using your LLM",
                    "BYOK token saved — your next intervention will use it.",
                )
            except Exception:
                logger.debug("BYOK info toast failed", exc_info=True)

    @Slot(str, dict)
    def _on_action_invoked(self, intervention_id: str, action: dict) -> None:
        """G4 (audit-prod): handle a desktop overlay action button click
        in WS mode.

        Native action types (clipboard, timer) execute in the shell
        directly. Everything else goes to the daemon as ACTION_EXECUTE
        with ``request_dispatch=True`` so the daemon broadcasts an
        ACTION_DISPATCH frame to the connected chrome / edge client.
        """
        if self._bridge is None:
            return
        action_type = str(action.get("action_type") or "")
        executed_natively = False
        if action_type == "copy_to_clipboard":
            try:
                from PySide6.QtGui import QGuiApplication

                clip = QGuiApplication.clipboard()
                target = str(action.get("target") or "")
                if clip is not None and target:
                    clip.setText(target)
                    executed_natively = True
            except Exception:
                logger.debug("Clipboard copy failed", exc_info=True)
        elif action_type == "start_timer":
            executed_natively = True

        # Audit-prod fix: ACTION_EXECUTE (with request_dispatch) must
        # arrive at the daemon BEFORE the engaged USER_ACTION. The
        # daemon's engage handler clears ``_active_intervention_id`` to
        # None as part of recording the outcome; ``dispatch_action_to_browser``
        # gates on that id being live, so the prior order silently
        # rejected every legitimate browser-action click.
        self._bridge.send_action_execute(
            intervention_id,
            dict(action),
            request_dispatch=not executed_natively,
        )
        # Then record engagement on the daemon (engaged USER_ACTION).
        self._bridge.send_user_action("engaged", intervention_id)

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

    @Slot()
    def _on_daemon_stop_requested(self) -> None:
        """Phase 4.B fix (#3): WS-mode equivalent of the in-process
        controller's daemon-stop slot. Sends the top-level ``SHUTDOWN``
        WS frame (with the bridge's cached capability token) so the
        daemon's ``_request_shutdown`` runs → which broadcasts
        SESSION_RECAP and then exits.

        Does NOT quit Qt. The dashboard's recap watchdog (or the recap
        sheet dismissal) will fire ``gui_quit_requested`` once the user
        has consumed the recap.
        """
        if self._bridge is None:
            logger.warning(
                "daemon_stop_requested: no bridge; falling through to quit"
            )
            self._on_gui_quit_requested()
            return
        try:
            self._bridge.send_shutdown()
        except Exception:
            logger.exception("send_shutdown raised")

    @Slot()
    def _on_gui_quit_requested(self) -> None:
        """Phase 4.B fix (#3): WS-mode equivalent of the controller's
        Qt-quit slot. Runs the regular ``_quit`` path."""
        logger.info("GUI quit requested — exiting Qt event loop (WS mode)")
        self._quit()

    def _on_user_initiated_quit(self) -> None:
        """Phase 4.B fix (#4): tray Quit / Cmd+W / SIGINT entry point.

        Routes through the dashboard's two-phase recap when a session is
        active so the user always sees their recap before the WS-mode
        shell exits. Falls through to a direct quit otherwise.

        Unlike the in-process controller we can't introspect the
        daemon directly — the daemon lives across the WS. We use the
        bridge's connection state plus the dashboard's stop-state as
        proxies for "is there an active session?".
        """
        bridge = self._bridge
        dashboard = self._dashboard
        if dashboard is None:
            self._on_gui_quit_requested()
            return
        consumer = getattr(dashboard, "_consumer", None)
        if consumer is None:
            self._on_gui_quit_requested()
            return
        if getattr(consumer, "_stopping", False):
            logger.debug(
                "user_initiated_quit: stop already armed; ignoring"
            )
            return
        # If we're not connected to the daemon, there's no recap to
        # wait for — quit directly.
        if bridge is None or not getattr(bridge, "_running", False):
            logger.debug(
                "user_initiated_quit: bridge not running; quitting directly"
            )
            self._on_gui_quit_requested()
            return
        logger.info(
            "user_initiated_quit: arming two-phase stop via consumer tab"
        )
        try:
            consumer._arm_stop()
        except Exception:
            logger.exception(
                "Failed to arm two-phase stop from user-initiated quit"
            )
            self._on_gui_quit_requested()

    @Slot(object, int)
    def _on_history_requested(self, since: object, limit: int) -> None:
        """P0 §3.1: forward a dashboard history request onto the WS wire.

        Phase 4.B fix (#28): on bridge-side failures post an empty error
        envelope through the bridge so the UI's loading state unsticks.
        """
        bridge = self._bridge
        if bridge is None:
            self._post_history_error("daemon_unavailable")
            return
        try:
            since_val = float(since) if since is not None else None
        except (TypeError, ValueError):
            since_val = None
        try:
            bridge.send_request_session_list(since_val, int(limit) if limit else 30)
        except Exception:
            logger.exception("send_request_session_list failed")
            self._post_history_error("send_failed")

    @Slot(str)
    def _on_detail_requested(self, session_id: str) -> None:
        """P0 §3.1: forward a dashboard detail request onto the WS wire."""
        bridge = self._bridge
        if bridge is None:
            self._post_detail_error("daemon_unavailable")
            return
        if not session_id:
            self._post_detail_error("missing_session_id")
            return
        try:
            bridge.send_request_session_detail(session_id)
        except Exception:
            logger.exception("send_request_session_detail failed")
            self._post_detail_error("send_failed")

    @Slot(str, bool)
    def _on_trends_requested(self, window: str, refresh: bool) -> None:
        """P0 §3.2: forward a dashboard trends request onto the WS wire."""
        bridge = self._bridge
        win = window if window in ("week", "month") else "week"
        if bridge is None:
            self._post_trends_error(win, "daemon_unavailable")
            return
        try:
            bridge.send_request_trends(win, bool(refresh))
        except Exception:
            logger.exception("send_request_trends failed")
            self._post_trends_error(win, "send_failed")

    def _post_history_error(self, reason: str) -> None:
        """Phase 4.B fix (#28): emit an empty-list error envelope so the
        History tab's loading state converts into an error state instead
        of staying stuck waiting for a SESSION_LIST that will never arrive.
        """
        if self._bridge is None:
            return
        try:
            self._bridge.session_list_received.emit({
                "items": [],
                "next_cursor": None,
                "total_known": 0,
                "error": reason,
            })
        except Exception:
            logger.debug(
                "history error envelope emit failed", exc_info=True
            )

    def _post_detail_error(self, reason: str) -> None:
        if self._bridge is None:
            return
        try:
            self._bridge.session_detail_received.emit({
                "report": None,
                "error": reason,
            })
        except Exception:
            logger.debug(
                "detail error envelope emit failed", exc_info=True
            )

    def _post_trends_error(self, window: str, reason: str) -> None:
        if self._bridge is None:
            return
        try:
            self._bridge.trends_received.emit({
                "window": window,
                "error": reason,
            })
        except Exception:
            logger.debug(
                "trends error envelope emit failed", exc_info=True
            )

    def _send_goal(self, goal: str) -> None:
        """Forward the dashboard goal-input text to the daemon."""
        if self._bridge is None or not goal:
            return
        try:
            self._bridge.send_user_action(f"set_goal:{goal}", "")
        except Exception:
            pass

    def _run_calibration(self) -> None:
        """Run calibration in-process on a worker thread.

        P0 §3.4 — replaces the legacy subprocess spawn. The WS-mode
        CortexApp does not own the daemon's event loop (the daemon is
        a separate process listening on 9473), so we spin up our own
        asyncio loop on a Qt worker thread and drive the
        ``CalibrationRunner`` there. Progress callbacks marshal onto
        the Qt main thread via the existing ``calibration_progress``
        signal on the WebSocket bridge.
        """
        from cortex.services.capture_service.calibration_runner import (
            CalibrationRunner,
        )

        if getattr(self, "_calibration_runner", None) is not None:
            logger.info("calibration already running; ignoring duplicate request")
            return

        def _progress_cb(progress: object) -> None:
            payload = {
                "elapsed_seconds": float(getattr(progress, "elapsed_seconds", 0.0)),
                "total_seconds": float(getattr(progress, "total_seconds", 0.0)),
                "current_hr": getattr(progress, "current_hr", None),
                "current_hrv": getattr(progress, "current_hrv", None),
                "current_sqi": getattr(progress, "current_sqi", None),
                "lighting_ok": bool(getattr(progress, "lighting_ok", False)),
                "motion_ok": bool(getattr(progress, "motion_ok", False)),
                "face_ok": bool(getattr(progress, "face_ok", False)),
                "pct_complete": float(getattr(progress, "pct_complete", 0.0)),
                "status": str(getattr(progress, "status", "running")),
            }
            try:
                if hasattr(self._bridge, "calibration_progress"):
                    self._bridge.calibration_progress.emit(payload)
                if self._onboarding is not None and hasattr(
                    self._onboarding, "apply_calibration_progress"
                ):
                    # In WS-mode the bridge may not carry a progress
                    # signal; call the slot directly (the runner thread
                    # is fine here because PySide6 queues cross-thread).
                    self._onboarding.apply_calibration_progress(**payload)
            except Exception:
                logger.debug("calibration progress emit failed", exc_info=True)

        runner = CalibrationRunner()
        self._calibration_runner = runner

        async def _drive() -> None:
            try:
                await runner.start(on_progress=_progress_cb)
                await runner.finish()
            except Exception:
                logger.exception("calibration run failed")
            finally:
                self._calibration_runner = None

        def _worker() -> None:
            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                loop.run_until_complete(_drive())
            finally:
                try:
                    loop.close()
                except Exception:
                    pass

        threading.Thread(
            target=_worker,
            name="cortex-calibration",
            daemon=True,
        ).start()

    def _on_calibration_progress(self, payload: dict) -> None:
        """Queue-connected slot — receives calibration progress on the
        Qt main thread. Forwards to the onboarding card and, on
        completion, refreshes the freshness pills on the dashboard and
        in Settings."""
        if self._onboarding is not None and hasattr(
            self._onboarding, "apply_calibration_progress"
        ):
            try:
                self._onboarding.apply_calibration_progress(**payload)
            except Exception:
                logger.debug("apply_calibration_progress failed", exc_info=True)
        if payload.get("status") == "completed":
            for surface in (self._dashboard, self._settings):
                if surface is not None and hasattr(
                    surface, "refresh_baseline_freshness"
                ):
                    try:
                        surface.refresh_baseline_freshness()
                    except Exception:
                        logger.debug("freshness refresh failed", exc_info=True)

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
