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
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from PySide6.QtCore import QObject, QTimer, Signal, Slot
from PySide6.QtWidgets import QApplication

from cortex.apps.desktop_shell import mac_native
from cortex.apps.desktop_shell.break_overlay import BreakOverlayWindow
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
    # P0 §3.1 / §3.2 / §3.3: history / trends / recap inbound payloads.
    # The daemon (in-process) calls these via :meth:`on_session_list`
    # etc. and they queue onto the Qt main thread before the dashboard's
    # apply_* methods run.
    session_list_received = Signal(dict)
    session_detail_received = Signal(dict)
    trends_received = Signal(dict)
    session_recap_received = Signal(dict)
    # P0 §3.11: daemon broadcast of the active quiet/pause mode.
    quiet_mode_state_received = Signal(dict)
    # P0 §3.10: daemon-armed focus session arm/disarm broadcast.
    auto_focus_armed_changed = Signal(bool, str)  # armed, preset
    # P0 §3.7 desktop dispatch: daemon-emitted "consider a break"
    # recommendation. Payload mirrors :attr:`MessageType.BREAK_RECOMMENDATION`
    # (reason, urgency, stress_load, threshold, duration_seconds,
    # breathing_pattern). Wired to the dashboard's break-pill in
    # CortexAppController so the UI nudges the user toward
    # ``take_biology_break`` without forcing the modal break overlay.
    break_recommendation_received = Signal(dict)
    # P0 §3.4: pushed at ~2 Hz from the CalibrationRunner so the
    # onboarding wizard's ECG trace, status pills, numerics, and bar all
    # stay in sync with the running capture loop. Payload is a plain
    # dict so Qt's queued-connection marshalling is cheap.
    calibration_progress = Signal(dict)

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

    def __init__(self) -> None:
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

    def on_session_list(self, payload: dict) -> None:
        """P0 §3.1: queue an inbound SESSION_LIST payload onto the Qt
        main thread. Safe to call from the daemon's asyncio loop or
        from a WS-receive callback — Qt signals marshal across threads."""
        try:
            self.session_list_received.emit(dict(payload) if payload else {})
        except Exception:
            logger.debug("session_list_received emit failed", exc_info=True)

    def on_session_detail(self, payload: dict) -> None:
        """P0 §3.1: queue an inbound SESSION_DETAIL payload."""
        try:
            self.session_detail_received.emit(dict(payload) if payload else {})
        except Exception:
            logger.debug("session_detail_received emit failed", exc_info=True)

    def on_trends(self, payload: dict) -> None:
        """P0 §3.2: queue an inbound TRENDS_PAYLOAD payload."""
        try:
            self.trends_received.emit(dict(payload) if payload else {})
        except Exception:
            logger.debug("trends_received emit failed", exc_info=True)

    def on_session_recap(self, payload: dict) -> None:
        """P0 §3.3: queue an inbound SESSION_RECAP payload.

        Called from the in-process broadcast observer (controller wraps
        ``_ws_server.send_message`` so SESSION_RECAP frames also fan
        out to this bridge in DMG mode). In WS-client mode the dashboard
        is wired off the ``WebSocketBridge`` directly, not this class.
        """
        try:
            self.session_recap_received.emit(dict(payload) if payload else {})
        except Exception:
            logger.debug("session_recap_received emit failed", exc_info=True)

    def on_quiet_mode_state(self, payload: dict) -> None:
        """P0 §3.11: queue an inbound QUIET_MODE_STATE payload."""
        try:
            self.quiet_mode_state_received.emit(dict(payload) if payload else {})
        except Exception:
            logger.debug("quiet_mode_state_received emit failed", exc_info=True)

    def on_start_focus_auto(self, payload: dict) -> None:
        """P0 §3.10: daemon armed an auto focus session — show the
        focus-protection pill on the dashboard."""
        try:
            preset = ""
            if isinstance(payload, dict):
                preset = str(payload.get("preset") or "")
            self.auto_focus_armed_changed.emit(True, preset)
        except Exception:
            logger.debug("on_start_focus_auto emit failed", exc_info=True)

    def on_stop_focus_auto(self, _payload: dict) -> None:
        """P0 §3.10: daemon disarmed the auto focus session — hide pill."""
        try:
            self.auto_focus_armed_changed.emit(False, "")
        except Exception:
            logger.debug("on_stop_focus_auto emit failed", exc_info=True)

    def on_break_recommendation(self, payload: dict) -> None:
        """P0 §3.7: relay BREAK_RECOMMENDATION onto the Qt main thread.

        Mirrors the other on_* handlers — dict is shallow-copied so the
        bridge can re-broadcast without callers mutating shared state.
        """
        try:
            self.break_recommendation_received.emit(
                dict(payload) if payload else {}
            )
        except Exception:
            logger.debug(
                "break_recommendation_received emit failed", exc_info=True,
            )

    def on_intervention_failed(self, payload: dict) -> None:
        """P1-FC-INTERVENTION-FAILED: a total mutation failure was
        broadcast by the daemon (``InterventionExecutor.apply`` returned
        only failed mutations, so the workspace was NOT changed). Surface
        it to the user via the existing error-toast path rather than
        letting the failure be silently invisible.

        Payload shape (per :attr:`MessageType.INTERVENTION_FAILED`):
        ``{intervention_id, error_reason, failed_action_types}``.
        """
        try:
            data = payload if isinstance(payload, dict) else {}
            reason = str(data.get("error_reason") or "").strip()
            failed = data.get("failed_action_types")
            if not reason:
                if isinstance(failed, (list, tuple)) and failed:
                    pretty = ", ".join(
                        str(a).replace("_", " ") for a in failed
                    )
                    reason = f"Couldn't apply: {pretty}."
                else:
                    reason = "Couldn't apply the intervention."
            cid = str(data.get("intervention_id") or "")
            self.on_error("Intervention couldn't be applied", reason, cid)
        except Exception:
            logger.debug("on_intervention_failed dispatch failed", exc_info=True)

    def on_intervention_prompt(self, payload: dict) -> None:
        """P1-FC-INTERVENTION-PROMPT: the daemon broadcast a cross-surface
        prompt (micro-commit / movement-break). The desktop overlay shows
        this inline already; this handler exists so the controller's
        dispatch map is complete and the prompt is at minimum logged for
        debug parity with the other surfaces. It deliberately does NOT
        duplicate the inline overlay widget.

        Payload shape (per :attr:`MessageType.INTERVENTION_PROMPT`):
        ``{action_type, prompt, timeout_seconds, metadata}``.
        """
        try:
            data = payload if isinstance(payload, dict) else {}
            logger.info(
                "INTERVENTION_PROMPT action_type=%s prompt=%r",
                data.get("action_type"),
                str(data.get("prompt") or "")[:120],
            )
        except Exception:
            logger.debug("on_intervention_prompt dispatch failed", exc_info=True)

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
        # P0 §3.4: in-flight calibration runner. None when idle. Guards
        # against double-click re-entry and lets ``_stop_daemon_and_quit``
        # cooperatively abort the runner on shutdown.
        self._calibration_runner: Any = None
        # Re-entrancy guard for :meth:`_on_daemon_stop_requested` — flipped
        # to True the first time a Stop click is processed and cleared by
        # the daemon-stopped callback. A double-click on Stop (or any
        # legacy back-compat signal that fans in to the same handler) is
        # then a no-op instead of re-scheduling ``daemon.stop()``.
        self._stopping: bool = False
        # P2 (audit-prod): teardown handle for the light/dark appearance
        # observer; kept alive for the controller lifetime.
        self._appearance_teardown: Callable[[], None] | None = None

    # -- public API -----------------------------------------------------------

    def run(self) -> int:
        """Create the Qt app, start the daemon, and enter the event loop."""
        _ensure_storage_dirs()

        self._app = QApplication(sys.argv)
        self._app.setApplicationName("Cortex")
        self._app.setOrganizationName("Cortex")
        self._app.setQuitOnLastWindowClosed(False)
        # Phase-3 P0-6 / P1-N1: cached focus state read by the
        # daemon-thread probe ``_desktop_is_focused_probe``. Initialised
        # to True so an early intervention doesn't OS-notify before the
        # first focusWindowChanged signal arrives.
        self._dashboard_is_focused: bool = True
        try:
            self._app.focusWindowChanged.connect(self._refresh_focus_cache)
        except Exception:
            logger.debug("focusWindowChanged signal wire failed", exc_info=True)

        # -- UI components ----------------------------------------------------
        self._dashboard = DashboardWindow()
        self._connections = ConnectionsPanel()
        self._overlay = OverlayWindow()
        # P0 §3.7: lazy — full-screen break overlay lives in its own
        # frameless window. Instantiated on first use so headless test
        # harnesses that never trigger a break don't pay the QSoundEffect
        # boot cost.
        self._break_overlay: BreakOverlayWindow | None = None
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
        # P0 §3.11: extra tray quiet-mode kinds (Quiet for session).
        if hasattr(self._tray, "quiet_mode_requested"):
            self._tray.quiet_mode_requested.connect(
                self._on_tray_quiet_mode_requested,
            )
        # Phase 4.B fix (#4): the tray Quit action (and Cmd+Q via the
        # native app menu) must honour the two-phase recap when a
        # session is active so the user always sees their recap before
        # the daemon shuts down. ``_on_user_initiated_quit`` arms the
        # recap flow when appropriate or falls through to a direct
        # quit when nothing is running.
        self._tray.quit_requested.connect(self._on_user_initiated_quit)

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
        # P0 §3.1 / §3.2 / §3.3: route history / trends / recap payloads
        # from the bridge straight into the dashboard's apply_* slots.
        # The dashboard delegates internally to the HistoryTab and the
        # RecapSheet so the wiring stays Tab-agnostic from this layer.
        self._bridge.session_list_received.connect(self._dashboard.apply_session_list)
        self._bridge.session_detail_received.connect(self._dashboard.apply_session_detail)
        self._bridge.trends_received.connect(self._dashboard.apply_trends)
        self._bridge.session_recap_received.connect(self._dashboard.apply_session_recap)
        # P0 §3.11 / §3.10: pipe QUIET_MODE_STATE + auto-focus broadcasts
        # into the dashboard's pause capsule + focus-protection pill.
        if hasattr(self._dashboard, "apply_quiet_mode_state"):
            self._bridge.quiet_mode_state_received.connect(
                self._dashboard.apply_quiet_mode_state,
            )
        if hasattr(self._dashboard, "apply_auto_focus_state"):
            self._bridge.auto_focus_armed_changed.connect(
                self._dashboard.apply_auto_focus_state,
            )
        # P0 §3.11: also fan QUIET_MODE_STATE into the tray menu so the
        # active kind shows a checkmark.
        self._bridge.quiet_mode_state_received.connect(
            self._on_quiet_mode_state_to_tray,
        )
        # P0 §3.7 desktop dispatch: route BREAK_RECOMMENDATION into the
        # dashboard's break pill (and tray notification helper). The
        # dashboard handler is the source of truth — if it isn't wired
        # (legacy lightweight test stub) we fall through silently.
        if hasattr(self._dashboard, "apply_break_recommendation"):
            self._bridge.break_recommendation_received.connect(
                self._dashboard.apply_break_recommendation,
            )

        self._overlay.dismissed.connect(self._on_overlay_dismissed)
        # G4 (audit-prod): overlay action buttons emit ``action_invoked``;
        # the in-process controller has a direct daemon reference so it
        # can route native actions (clipboard, timer) directly and call
        # ``dispatch_action_to_browser`` for everything else.
        if hasattr(self._overlay, "action_invoked"):
            self._overlay.action_invoked.connect(self._on_action_invoked)
        # P0 §3.6: micro-step checkbox round-trip. The overlay emits
        # ``micro_step_toggled(intervention_id, step_index, new_status)``
        # for every checkbox click; we forward to the daemon which
        # mutates the active plan and rebroadcasts ``INTERVENTION_TRIGGER``
        # so peer surfaces re-render the strikethrough.
        if hasattr(self._overlay, "micro_step_toggled"):
            self._overlay.micro_step_toggled.connect(self._on_micro_step_toggled)
        # P0 §3.8: rating button round-trip — the overlay emits
        # ``rating_invoked(intervention_id, rating, text_feedback)``;
        # the controller forwards via USER_RATING to the daemon.
        if hasattr(self._overlay, "rating_invoked"):
            self._overlay.rating_invoked.connect(self._on_rating_invoked)
        # P0 §3.9: "Why?" chevron — when expanded the first time we
        # send WHY_DETAIL_REQUEST and pipe the resulting structured
        # signals back into the overlay's panel.
        if hasattr(self._overlay, "why_requested"):
            self._overlay.why_requested.connect(self._on_why_requested)
        # P0 §3.11: overlay footer snooze / quiet buttons. The overlay
        # has already triggered its own dismiss flow; we additionally
        # forward the kind to the daemon's set_quiet_mode so every
        # surface picks up the new QUIET_MODE_STATE broadcast.
        if hasattr(self._overlay, "quiet_requested"):
            self._overlay.quiet_requested.connect(self._on_quiet_requested)
        self._settings.settings_changed.connect(self._on_settings_changed)
        self._settings.back_requested.connect(self._show_dashboard)
        self._connections.back_requested.connect(self._show_dashboard)
        self._onboarding.open_settings_requested.connect(self._show_settings)
        self._onboarding.run_calibration_requested.connect(self._run_calibration)
        self._onboarding.completed.connect(self._complete_onboarding)
        # P0 §3.4: route calibration progress callbacks from the runner
        # (emitted on the daemon thread) back to the onboarding card's
        # apply_calibration_progress slot. Queued connection by default —
        # Qt marshals the dict payload onto the Qt main thread.
        self._bridge.calibration_progress.connect(self._on_calibration_progress)
        # P0 §3.4: Settings → Sensing → Recalibrate baselines. Re-uses
        # the same _run_calibration path as onboarding so both surfaces
        # drive one CalibrationRunner.
        if hasattr(self._settings, "recalibrate_requested"):
            self._settings.recalibrate_requested.connect(self._run_calibration)
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
        # Audit-prod fix (P1-2 + P1-3): wire the previously-orphan
        # Settings signals. ``settings_save_failed`` surfaces save
        # errors in the dashboard toast; ``auth_token_rotated`` tells
        # the in-process daemon to re-read the token file (the
        # in-process WS server reads ``load_or_create_token`` once at
        # startup; without this hook a rotation appears successful but
        # the next session still tries the old token).
        if hasattr(self._settings, "settings_save_failed"):
            self._settings.settings_save_failed.connect(
                self._on_settings_save_failed
            )
        if hasattr(self._settings, "auth_token_rotated"):
            self._settings.auth_token_rotated.connect(
                self._on_auth_token_rotated
            )
        # E.1 / Phase 4.B fix (#2): dashboard Stop button + goal input.
        # The two-signal split (``daemon_stop_requested`` / ``gui_quit_requested``)
        # fixes the DMG stop deadlock:
        #
        # * ``daemon_stop_requested`` fires immediately on the Stop click
        #   → ``_on_daemon_stop_requested`` schedules ``daemon.stop()`` on
        #   the daemon-thread loop. This kicks off the SESSION_RECAP
        #   broadcast pipeline; we do NOT quit Qt here.
        #
        # * ``gui_quit_requested`` fires after the recap is consumed
        #   (dismissed by user, watchdog, safety timer) → ``_on_gui_quit_requested``
        #   runs the regular ``_quit`` path that closes windows and exits Qt.
        #
        # Backwards-compat: the legacy ``stop_requested`` signal is an
        # alias of ``daemon_stop_requested`` (see dashboard.py). Connect
        # it to the same daemon-stop slot so existing call sites keep
        # behaving correctly; the quit path is gated solely on
        # ``gui_quit_requested``.
        if hasattr(self._dashboard, "daemon_stop_requested"):
            self._dashboard.daemon_stop_requested.connect(
                self._on_daemon_stop_requested,
            )
        if hasattr(self._dashboard, "gui_quit_requested"):
            self._dashboard.gui_quit_requested.connect(
                self._on_gui_quit_requested,
            )
        # The legacy ``stop_requested`` signal is no longer wired here —
        # ``daemon_stop_requested`` (connected just above) is the canonical
        # path. Both signals are still emitted by the dashboard for
        # back-compat, but the duplicate connection would route a single
        # Stop click into ``_on_daemon_stop_requested`` twice, scheduling
        # two redundant ``daemon.stop()`` futures. The ``_stopping`` guard
        # inside that handler is now the second line of defence.
        if hasattr(self._dashboard, "goal_set"):
            self._dashboard.goal_set.connect(self._on_goal_set)
        # Audit-prod fix (P0): wire the previously-orphan dashboard
        # signals so the user-facing affordances actually drive the
        # daemon.
        #
        # * ``break_pill_clicked(payload)`` — Take-a-break pill click.
        #   Routes through the same ``USER_ACTION`` channel the browser
        #   extension uses so the daemon's break controller can promote
        #   the recommendation to a full breathing session.
        # * ``undo_action_requested(intervention_id)`` — Undo toast +
        #   Restore previous state pill. We submit an ``undone``
        #   USER_ACTION so the restore manager rolls back the most
        #   recent reversible mutation.
        # * ``force_recap_requested`` — Cmd+Shift+R developer shortcut.
        #   Calls ``daemon.force_recap`` (P0 §3.21).
        # * ``dismiss_overlay_requested`` — Cmd+Shift+D developer
        #   shortcut. Calls ``daemon.dismiss_active_overlay`` (P0 §3.21).
        if hasattr(self._dashboard, "break_pill_clicked"):
            self._dashboard.break_pill_clicked.connect(
                self._on_break_pill_clicked,
            )
        if hasattr(self._dashboard, "undo_action_requested"):
            self._dashboard.undo_action_requested.connect(
                self._on_undo_action_requested,
            )
        if hasattr(self._dashboard, "force_recap_requested"):
            self._dashboard.force_recap_requested.connect(
                self._on_force_recap_requested,
            )
        if hasattr(self._dashboard, "dismiss_overlay_requested"):
            self._dashboard.dismiss_overlay_requested.connect(
                self._on_dismiss_overlay_requested,
            )
        # P0 §3.19: settings → daemon TEST_PROVIDER round-trip.
        if hasattr(self._settings, "test_provider_requested"):
            self._settings.test_provider_requested.connect(
                self._on_test_provider_requested,
            )
        # P0 §3.1 / §3.2: route outgoing history/trends requests from the
        # dashboard to the daemon. In-process mode can call the daemon's
        # async methods directly via run_coroutine_threadsafe; the
        # result is funnelled back through the bridge so the same
        # incoming-payload path serves both modes.
        if hasattr(self._dashboard, "history_requested"):
            self._dashboard.history_requested.connect(self._on_history_requested)
        if hasattr(self._dashboard, "detail_requested"):
            self._dashboard.detail_requested.connect(self._on_detail_requested)
        if hasattr(self._dashboard, "trends_requested"):
            self._dashboard.trends_requested.connect(self._on_trends_requested)
        # P0 §3.11: dashboard pause/quiet menu picks → daemon.
        if hasattr(self._dashboard, "quiet_mode_requested"):
            self._dashboard.quiet_mode_requested.connect(
                self._on_dashboard_quiet_requested,
            )
        # P0 §3.10: auto-focus "Turn off" toast click → daemon.
        if hasattr(self._dashboard, "auto_focus_disarm_requested"):
            self._dashboard.auto_focus_disarm_requested.connect(
                self._on_auto_focus_disarm_requested,
            )
        # P0 §3.3 (Wave-2 P1): recap dismiss → daemon ACK. The daemon's
        # ``stop()`` waits up to 5 s on ``acknowledge_session_recap`` so
        # the WS teardown can't race a fast UI hide. Without this
        # bridge, every in-process shutdown eats the full 5 s timeout.
        if hasattr(self._dashboard, "recap_dismissed_ack"):
            self._dashboard.recap_dismissed_ack.connect(
                self._on_recap_dismissed_ack,
            )

        # -- Start daemon in background thread --------------------------------
        self._start_daemon()

        # -- Graceful shutdown ------------------------------------------------
        # Phase 4.B fix (#4): ``aboutToQuit`` is non-cancellable, so the
        # recap routing has to happen BEFORE the quit decision. We hook
        # ``lastWindowClosed`` (fires when the user clicks the macOS
        # window close button on the dashboard while no other top-level
        # window is open) and route it through ``_on_user_initiated_quit``
        # which decides whether to arm the recap or quit directly.
        # ``aboutToQuit`` keeps its existing role as the safety-net
        # daemon shutdown — it fires AFTER ``_on_gui_quit_requested``
        # → ``_quit`` → ``app.quit()``.
        try:
            self._app.lastWindowClosed.connect(self._on_user_initiated_quit)
        except Exception:
            logger.debug("lastWindowClosed connect failed", exc_info=True)
        self._app.aboutToQuit.connect(self._shutdown_daemon)
        # ``setQuitOnLastWindowClosed`` was set to False above so the
        # tray keeps Cortex alive when the user closes the dashboard.
        # We re-enable the implicit quit chain via lastWindowClosed so
        # Cmd+W on the dashboard still routes through the recap flow.
        signal.signal(signal.SIGINT, lambda *_: self._on_user_initiated_quit())
        signal.signal(signal.SIGTERM, lambda *_: self._on_user_initiated_quit())
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
            from AppKit import (
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
            # P2 (audit-prod): wire the light/dark appearance observer so
            # the native window background re-adapts on a System Settings
            # theme flip without a restart.
            self._install_appearance_observer()

        QTimer.singleShot(200, _initial_show)

        return self._app.exec()

    def _install_appearance_observer(self) -> None:
        """Install the macOS light/dark appearance observer; re-apply the
        native window chrome (background + titlebar) to every top-level
        window on a theme flip. The retint is marshalled to the Qt main
        thread via ``QTimer.singleShot(0, ...)`` because the observer
        callback fires on the AppKit notification queue."""
        def _on_flip(_is_dark: bool) -> None:
            def _retint() -> None:
                for window in (self._dashboard, self._settings,
                               self._overlay, self._onboarding):
                    if window is None:
                        continue
                    try:
                        mac_native.apply_vibrancy(window)
                        mac_native.apply_unified_titlebar(window)
                    except Exception:
                        logger.debug("appearance retint failed", exc_info=True)
            try:
                QTimer.singleShot(0, _retint)
            except Exception:
                _retint()
        try:
            self._appearance_teardown = mac_native.install_appearance_observer(
                _on_flip
            )
        except Exception:
            logger.debug("install_appearance_observer failed", exc_info=True)
            self._appearance_teardown = None

    # -- Daemon lifecycle -----------------------------------------------------

    def _start_daemon(self) -> None:
        """Boot the CortexDaemon in a background thread."""
        # Lazy import to keep module-level imports light
        from cortex.services.runtime_daemon import CortexDaemon

        self._daemon = CortexDaemon(config=self._config)
        self._daemon.set_state_callback(self._bridge.on_state)
        self._daemon.set_intervention_callback(self._bridge.on_intervention)
        # P0 §3.7: hand the desktop shell's full-screen break overlay to
        # the daemon. The handler is async because the controller has
        # to marshal back onto the Qt thread; the daemon owns the
        # asyncio loop and the BiologyBreakController calls our handler
        # whenever the user takes a break.
        self._daemon.set_break_overlay_ui_handler(self._run_break_overlay)
        # P0 §3.12: register the desktop-focus probe so the daemon
        # knows when to dispatch OS-level notification fallbacks.
        try:
            self._daemon.set_desktop_focus_probe(self._desktop_is_focused_probe)
        except Exception:
            logger.debug("set_desktop_focus_probe failed", exc_info=True)
        # P0 §3.3: subscribe to SESSION_RECAP broadcasts.
        # The daemon emits SESSION_RECAP exclusively via
        # ``_ws_server.send_message`` (see runtime_daemon.stop()'s 90 s
        # report finalisation block). In-process mode never opens a WS
        # client, so we hook the same path by wrapping ``send_message``
        # with a thin observer that forwards SESSION_LIST /
        # SESSION_DETAIL / TRENDS_PAYLOAD / SESSION_RECAP frames into
        # the bridge while preserving the original call's semantics
        # for WS-attached clients.
        self._install_ws_broadcast_observer()

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

    def _install_ws_broadcast_observer(self) -> None:
        """P0 §3.1 / §3.2 / §3.3: wrap ``_ws_server.send_message`` so the
        in-process controller observes outbound SESSION_LIST /
        SESSION_DETAIL / TRENDS_PAYLOAD / SESSION_RECAP frames in
        addition to relaying them to attached WS clients.

        Why a wrapper, not a callback API? The daemon's WS server has no
        broadcast-observer hook today (the existing
        ``set_state_callback`` / ``set_intervention_callback`` mechanism
        is daemon-internal and predates these frames). A monkey-patched
        wrapper is the smallest non-invasive way to keep the backend
        contract intact while still surfacing the frames to the
        in-process dashboard. The wrapper preserves the original return
        value (a client count) and is idempotent — re-installs are
        no-ops via the ``_cortex_broadcast_wrapped`` sentinel attr.
        """
        if self._daemon is None:
            return
        ws_server = getattr(self._daemon, "_ws_server", None)
        if ws_server is None:
            logger.debug("Daemon has no _ws_server; broadcast observer skipped")
            return
        send_message = getattr(ws_server, "send_message", None)
        if send_message is None or getattr(send_message, "_cortex_broadcast_wrapped", False):
            return
        bridge = self._bridge

        # Map of message-type strings → bridge methods. We import the
        # enum locally so a missing schemas package on a stripped CI
        # harness doesn't crash boot — the wrapper degrades to passing
        # through without fan-out in that case.
        try:
            from cortex.libs.schemas.ws_message_types import MessageType

            type_to_handler = {
                MessageType.SESSION_LIST.value: bridge.on_session_list,
                MessageType.SESSION_DETAIL.value: bridge.on_session_detail,
                MessageType.TRENDS_PAYLOAD.value: bridge.on_trends,
                MessageType.SESSION_RECAP.value: bridge.on_session_recap,
                # P0 §3.11 / §3.10: route quiet-mode + auto-focus
                # broadcasts to the bridge so the dashboard reflects
                # them in DMG mode (the WS-client path picks them up
                # via the WebSocketBridge separately).
                MessageType.QUIET_MODE_STATE.value: bridge.on_quiet_mode_state,
                MessageType.START_FOCUS_AUTO.value: bridge.on_start_focus_auto,
                MessageType.STOP_FOCUS_AUTO.value: bridge.on_stop_focus_auto,
                # P0 §3.7 desktop dispatch.
                MessageType.BREAK_RECOMMENDATION.value: bridge.on_break_recommendation,
                # P1-FC-INTERVENTION-FAILED: total mutation failure → toast.
                MessageType.INTERVENTION_FAILED.value: bridge.on_intervention_failed,
                # P1-FC-INTERVENTION-PROMPT: cross-surface prompt sync. The
                # desktop overlay renders the prompt inline already; this
                # entry only completes the dispatch map (informational).
                MessageType.INTERVENTION_PROMPT.value: bridge.on_intervention_prompt,
            }
        except Exception:
            logger.debug("MessageType import failed; broadcast observer disabled", exc_info=True)
            return

        async def _wrapped_send_message(message_type: str, payload: dict, **kwargs: Any) -> int:
            # Phase 4.B fix (#26): respect ``target_client_types``.
            # The daemon's WS dispatch arms now use
            # ``send_to_client(client_id, ...)`` for targeted replies, so
            # this observer should normally only see broadcasts. But a
            # caller that goes through ``send_message`` with a non-empty
            # ``target_client_types`` list that excludes "desktop" is
            # explicitly opting out of the in-process bridge; we must
            # honour that to avoid leaking targeted SESSION_RECAP
            # replies into the desktop dashboard.
            targets = kwargs.get("target_client_types")
            if isinstance(targets, (list, tuple, set)) and targets:
                if "desktop" not in targets:
                    logger.debug(
                        "broadcast observer: skipping %s — targets=%r excludes 'desktop'",
                        message_type, targets,
                    )
                    return await send_message(message_type, payload, **kwargs)
            handler = type_to_handler.get(message_type)
            if handler is not None:
                try:
                    handler(dict(payload) if payload else {})
                except Exception:
                    logger.debug(
                        "Broadcast observer handler raised for %s",
                        message_type, exc_info=True,
                    )
            return await send_message(message_type, payload, **kwargs)

        _wrapped_send_message._cortex_broadcast_wrapped = True  # type: ignore[attr-defined]
        try:
            ws_server.send_message = _wrapped_send_message
        except Exception:
            logger.debug(
                "Failed to install ws_server.send_message wrapper",
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # P0 §3.1 / §3.2: outbound history/trends/detail request handlers.
    # ------------------------------------------------------------------

    @Slot(object, int)
    def _on_history_requested(self, since: object, limit: int) -> None:
        """Dashboard wants a paginated session listing. In-process mode
        calls ``daemon.list_sessions`` directly on the daemon loop and
        funnels the response back through ``bridge.on_session_list``
        so the same Qt-signal path serves both modes.

        Phase 4.B fix (#28): every exception path posts an empty error
        envelope to the bridge so the History tab's loading state
        unsticks. The previous implementation silently swallowed
        exceptions, leaving the user staring at an empty list.
        """
        bridge = self._bridge
        if self._daemon is None or self._daemon_loop is None:
            bridge.on_session_list(
                {"items": [], "next_cursor": None, "total_known": 0,
                 "error": "daemon_unavailable"}
            )
            return
        try:
            since_val: float | None = None
            if since is not None:
                since_val = float(since)
        except (TypeError, ValueError):
            since_val = None
        limit_val = int(limit) if limit else 30

        async def _run() -> None:
            try:
                resp = await self._daemon.list_sessions(since_val, limit_val)
            except Exception:
                logger.exception("list_sessions failed")
                try:
                    bridge.on_session_list(
                        {"items": [], "next_cursor": None, "total_known": 0,
                         "error": "internal"}
                    )
                except Exception:
                    logger.debug(
                        "error envelope dispatch failed", exc_info=True
                    )
                return
            payload = (
                resp.model_dump(mode="json")
                if hasattr(resp, "model_dump") else dict(resp or {})
            )
            try:
                bridge.on_session_list(payload)
            except Exception:
                logger.debug(
                    "bridge.on_session_list dispatch failed", exc_info=True
                )

        try:
            asyncio.run_coroutine_threadsafe(_run(), self._daemon_loop)
        except Exception:
            logger.exception("list_sessions schedule failed")
            try:
                bridge.on_session_list(
                    {"items": [], "next_cursor": None, "total_known": 0,
                     "error": "internal"}
                )
            except Exception:
                logger.debug(
                    "error envelope dispatch failed", exc_info=True
                )

    @Slot(str)
    def _on_detail_requested(self, session_id: str) -> None:
        """Phase 4.B fix (#28): on any failure, post a ``{"report": None,
        "error": ...}`` envelope so the detail panel's loading spinner
        converts into a clear error state instead of staying stuck."""
        bridge = self._bridge
        if self._daemon is None or self._daemon_loop is None:
            bridge.on_session_detail(
                {"report": None, "error": "daemon_unavailable"}
            )
            return
        if not session_id:
            bridge.on_session_detail(
                {"report": None, "error": "missing_session_id"}
            )
            return

        async def _run() -> None:
            try:
                resp = await self._daemon.get_session(session_id)
            except Exception:
                logger.exception("get_session failed")
                try:
                    bridge.on_session_detail(
                        {"report": None, "error": "internal"}
                    )
                except Exception:
                    logger.debug(
                        "detail error envelope dispatch failed", exc_info=True
                    )
                return
            payload = (
                resp.model_dump(mode="json")
                if hasattr(resp, "model_dump") else dict(resp or {})
            )
            try:
                bridge.on_session_detail(payload)
            except Exception:
                logger.debug(
                    "bridge.on_session_detail dispatch failed", exc_info=True
                )

        try:
            asyncio.run_coroutine_threadsafe(_run(), self._daemon_loop)
        except Exception:
            logger.exception("get_session schedule failed")
            try:
                bridge.on_session_detail(
                    {"report": None, "error": "internal"}
                )
            except Exception:
                logger.debug(
                    "detail error envelope dispatch failed", exc_info=True
                )

    @Slot(str, bool)
    def _on_trends_requested(self, window: str, refresh: bool) -> None:
        """Phase 4.B fix (#18): drop the "quarter" alternative — the
        schema is ``Literal["week", "month"]`` and the dashboard never
        emits "quarter". Unknown windows degrade to "week" so the
        request still completes."""
        bridge = self._bridge
        win = window if window in ("week", "month") else "week"
        if self._daemon is None or self._daemon_loop is None:
            bridge.on_trends({"window": win, "error": "daemon_unavailable"})
            return

        async def _run() -> None:
            try:
                resp = await self._daemon.get_trends(win, refresh=bool(refresh))
            except Exception:
                logger.exception("get_trends failed")
                try:
                    bridge.on_trends({"window": win, "error": "internal"})
                except Exception:
                    logger.debug(
                        "trends error envelope dispatch failed", exc_info=True
                    )
                return
            payload = (
                resp.model_dump(mode="json")
                if hasattr(resp, "model_dump") else dict(resp or {})
            )
            try:
                bridge.on_trends(payload)
            except Exception:
                logger.debug(
                    "bridge.on_trends dispatch failed", exc_info=True
                )

        try:
            asyncio.run_coroutine_threadsafe(_run(), self._daemon_loop)
        except Exception:
            logger.exception("get_trends schedule failed")
            try:
                bridge.on_trends({"window": win, "error": "internal"})
            except Exception:
                logger.debug(
                    "trends error envelope dispatch failed", exc_info=True
                )

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
        # Phase-3 P0-DF-12.2 (dual-fire de-dup): when the daemon
        # already stamped ``desktop_not_focused`` we KNOW the OS-level
        # notification path is firing. Surface the cue *only* via the
        # notification — popping the overlay on a hidden / cross-Space
        # window flashes nothing visible but does keep the dashboard
        # "armed" with a pending intervention that the user can't see.
        # When the user comes back, the standard re-mount path on
        # ``handleIntervention`` (via stored ``activeIntervention``)
        # restores the overlay; here we just skip the immediate show.
        if payload.get("desktop_not_focused") is True:
            return
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
        # Clear the re-entrancy latch so a subsequent Start → Stop cycle
        # within the same Qt process is honoured (e.g. test harnesses,
        # future hot-restart support).
        self._stopping = False
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

    @Slot(str, dict)
    def _on_action_invoked(self, intervention_id: str, action: dict) -> None:
        """G4 (audit-prod): handle a desktop overlay action button click.

        Native action types execute in the desktop shell directly so the
        user gets immediate feedback without a WS roundtrip; browser-bound
        actions are forwarded to chrome/edge via the daemon's
        ``dispatch_action_to_browser`` helper. Either way we record an
        engaged USER_ACTION on the daemon so the dismissal/engagement
        ledger reflects what happened.
        """
        if self._daemon is None or self._daemon_loop is None:
            return
        action_type = str(action.get("action_type") or "")
        action_id = str(action.get("action_id") or "")
        # Native vs browser routing is authoritative on the overlay's
        # frozensets so a single source of truth governs which clicks
        # reach ``dispatch_action_to_browser``. Anything NOT in
        # ``OverlayWindow._BROWSER_ACTION_TYPES`` must execute natively
        # (or via a daemon-local helper) — previously every native type
        # except copy/timer fell through to the browser dispatch path
        # and was dropped (the SuggestedAction-bound extension never
        # recognised ``take_biology_break`` / ``resume_last_active_file``
        # / ``prompt_micro_commit`` / ``suggest_movement_break``).
        is_browser_action = action_type in OverlayWindow._BROWSER_ACTION_TYPES
        executed_natively = not is_browser_action
        # A daemon-local coroutine to run for the native action types
        # that need real daemon-side work (a break session, an editor
        # focus, or a prompt broadcast). ``None`` means "log only".
        native_coro: Any = None
        try:
            if action_type == "copy_to_clipboard":
                self._copy_to_clipboard(str(action.get("target") or ""))
            elif action_type == "start_timer":
                # P2-FE-START-TIMER: a real LLM-emitted native action.
                # Reuse the existing break/countdown overlay
                # (BreakOverlayWindow, driven by the daemon's break
                # controller) as a plain countdown — no breathing pattern
                # and no audio cue. The duration comes from the action
                # metadata; absent that we fall back to a 5-minute timer.
                meta = action.get("metadata")
                meta = meta if isinstance(meta, dict) else {}
                duration = int(meta.get("duration_seconds", 300) or 300)
                native_coro = self._daemon.start_biology_break(
                    intervention_id=str(intervention_id or ""),
                    duration_seconds=duration,
                    breathing_pattern=None,
                    audio_cue=False,
                    reason=str(meta.get("reason", "user_requested_timer"))[:120],
                )
            elif action_type == "take_biology_break":
                # P0 §3.7: the breathing session is a full-screen Qt
                # overlay driven by the daemon (it owns the HRV context).
                # Route to ``start_biology_break`` — NEVER the browser.
                meta = action.get("metadata")
                meta = meta if isinstance(meta, dict) else {}
                duration = int(meta.get("duration_seconds", 240) or 240)
                pattern = meta.get("breathing_pattern")
                native_coro = self._daemon.start_biology_break(
                    intervention_id=str(intervention_id or ""),
                    duration_seconds=duration,
                    breathing_pattern=pattern if isinstance(pattern, str) else None,
                    audio_cue=bool(meta.get("audio_cue", True)),
                    reason=str(meta.get("reason", "user_requested_break"))[:120],
                )
            elif action_type == "resume_last_active_file":
                # Editor/native adapter — focus the last active file in
                # the connected VS Code / editor adapter.
                native_coro = self._daemon._resume_last_active_file(dict(action))
            elif action_type in ("prompt_micro_commit", "suggest_movement_break"):
                # Native inline widgets (confirmed in the overlay). Mirror
                # the engagement to peer surfaces via a prompt broadcast,
                # then record natively. Nothing is dispatched to chrome.
                params = {
                    k: v for k, v in action.items()
                    if k not in ("action_type", "action_id", "label", "reason")
                }
                native_coro = self._daemon._broadcast_prompt(action_type, params)
        except Exception:
            logger.debug("Native action execution failed", exc_info=True)

        # Audit-prod fix: dispatch + engage + log are composed into a
        # single coroutine so the ordering invariant ("dispatch BEFORE
        # engage") is enforced lexically by ``await``, not implicitly by
        # FIFO scheduling of three separate ``run_coroutine_threadsafe``
        # calls. The engage handler invokes ``_restore_manager.engage``
        # which clears ``_active_intervention_id``; the dispatch
        # liveness gate reads that same field. If the gate ran after
        # engage, every legitimate browser-action click would be
        # rejected as stale.
        action_copy = dict(action)
        log_payload = {
            "action_id": action_id,
            "action_type": action_type,
            "intervention_id": intervention_id,
            "result": {"native": executed_natively, "source": "desktop_overlay"},
        }
        engage_payload = {
            "action": "engaged",
            "intervention_id": intervention_id,
        }

        async def _dispatch_then_record() -> None:
            if executed_natively:
                # Native action types run a daemon-local coroutine (break
                # session / countdown timer / editor focus / prompt
                # broadcast) when one was prepared; copy_to_clipboard is
                # log-only (handled synchronously above).
                if native_coro is not None:
                    try:
                        await native_coro
                    except Exception:
                        logger.debug(
                            "native action coroutine failed", exc_info=True,
                        )
            else:
                try:
                    await self._daemon.dispatch_action_to_browser(
                        intervention_id, action_copy,
                    )
                except Exception:
                    logger.debug(
                        "dispatch_action_to_browser failed", exc_info=True,
                    )
            try:
                await self._daemon._handle_user_action(engage_payload)
            except Exception:
                logger.debug(
                    "Engaged USER_ACTION submission failed", exc_info=True,
                )
            try:
                await self._daemon._handle_user_action(log_payload)
            except Exception:
                logger.debug(
                    "action_executed log submission failed", exc_info=True,
                )

        try:
            asyncio.run_coroutine_threadsafe(
                _dispatch_then_record(), self._daemon_loop,
            )
        except Exception:
            logger.debug("dispatch/engage scheduling failed", exc_info=True)

    @Slot(str, int, str)
    def _on_micro_step_toggled(
        self, intervention_id: str, step_index: int, new_status: str,
    ) -> None:
        """P0 §3.6: forward a desktop-overlay micro-step toggle to the
        daemon. The daemon mutates the active plan, rebroadcasts
        ``INTERVENTION_TRIGGER`` so peer surfaces re-render, and fires
        ``natural_recovery`` once every step is ``"done"``.
        """
        if self._daemon is None or self._daemon_loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._daemon.toggle_micro_step(
                    str(intervention_id),
                    int(step_index),
                    str(new_status),
                ),
                self._daemon_loop,
            )
        except Exception:
            logger.debug(
                "toggle_micro_step scheduling failed", exc_info=True
            )

    @Slot(str, str, str)
    def _on_rating_invoked(
        self, intervention_id: str, rating: str, text_feedback: str,
    ) -> None:
        """P0 §3.8: forward 👍/👎 to the daemon via USER_RATING handler."""
        if self._daemon is None or self._daemon_loop is None:
            return
        payload = {
            "intervention_id": str(intervention_id),
            "rating": str(rating),
        }
        if text_feedback:
            payload["context"] = str(text_feedback)[:200]
        try:
            asyncio.run_coroutine_threadsafe(
                self._daemon._handle_user_action(payload),
                self._daemon_loop,
            )
        except Exception:
            logger.debug("rating_invoked scheduling failed", exc_info=True)

    @Slot(str)
    def _on_why_requested(self, intervention_id: str) -> None:
        """P0 §3.9: resolve a WHY_DETAIL_REQUEST locally and apply the
        signals to the overlay's drilldown panel.
        """
        if self._daemon is None or self._daemon_loop is None:
            return

        async def _fetch_and_apply() -> None:
            try:
                signals = await self._daemon.get_causal_signals(
                    str(intervention_id),
                )
            except Exception:
                logger.debug("get_causal_signals failed", exc_info=True)
                signals = None
            if signals is None:
                signals = []
            # Marshal back onto the Qt thread.
            from PySide6.QtCore import QTimer as _QTimer

            def _apply() -> None:
                if self._overlay is not None and hasattr(
                    self._overlay, "apply_causal_signals"
                ):
                    try:
                        self._overlay.apply_causal_signals(list(signals))
                    except Exception:
                        logger.debug("apply_causal_signals failed", exc_info=True)

            _QTimer.singleShot(0, _apply)

        try:
            asyncio.run_coroutine_threadsafe(
                _fetch_and_apply(), self._daemon_loop,
            )
        except Exception:
            logger.debug("why_requested scheduling failed", exc_info=True)

    def _on_tray_quiet_mode_requested(
        self, kind: str, duration_minutes: int,
    ) -> None:
        """Tray submenu quiet-mode pick → daemon.set_quiet_mode."""
        minutes: int | None = (
            int(duration_minutes) if duration_minutes and duration_minutes > 0 else None
        )
        self.request_quiet_mode(kind, duration_minutes=minutes, source="tray")

    def _on_quiet_mode_state_to_tray(self, payload: dict) -> None:
        """Mirror QUIET_MODE_STATE onto the tray menu's checkable items."""
        if self._tray is None or not isinstance(payload, dict):
            return
        kind = str(payload.get("kind", "off"))
        try:
            self._tray.set_quiet_mode_kind(kind)
        except Exception:
            logger.debug("tray.set_quiet_mode_kind failed", exc_info=True)

    def _on_dashboard_quiet_requested(
        self, kind: str, duration_minutes: int,
    ) -> None:
        """Dashboard pause/quiet menu pick → daemon.set_quiet_mode."""
        self.request_quiet_mode(
            kind,
            duration_minutes=(
                int(duration_minutes) if duration_minutes and duration_minutes > 0 else None
            ),
            source="dashboard",
        )

    def _on_auto_focus_disarm_requested(self) -> None:
        """Dashboard auto-focus "Turn off" toast → daemon.disarm_auto_focus."""
        if self._daemon is None or self._daemon_loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._daemon.disarm_auto_focus(), self._daemon_loop,
            )
        except Exception:
            logger.debug("disarm_auto_focus scheduling failed", exc_info=True)

    @Slot(str)
    def _on_recap_dismissed_ack(self, session_id: str) -> None:
        """P0 §3.3 (Wave-2 P1): forward the recap dismiss ack to the daemon.

        ``daemon.stop()`` waits up to 5 s on
        ``acknowledge_session_recap`` before tearing down the WS server
        so a fast UI hide doesn't race the shutdown. The in-process
        recap sheet emits ``recap_dismissed_ack`` straight after the
        user clicks Close (or the autohide fires); the controller
        forwards it onto the daemon's loop here.
        """
        if self._daemon is None or self._daemon_loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._daemon.acknowledge_session_recap(
                    str(session_id) if session_id else None,
                ),
                self._daemon_loop,
            )
        except Exception:
            logger.debug(
                "acknowledge_session_recap scheduling failed", exc_info=True
            )

    def _on_quiet_requested(self, kind: str, duration_minutes: int) -> None:
        """P0 §3.11: overlay (or dashboard menu / shortcut) asked the
        daemon to enter a quiet / pause mode.

        Forward to ``RuntimeDaemon.set_quiet_mode`` so the unified
        QUIET_MODE_STATE broadcast updates every surface.
        """
        if self._daemon is None or self._daemon_loop is None:
            return
        minutes: int | None = (
            int(duration_minutes) if duration_minutes and duration_minutes > 0 else None
        )
        try:
            asyncio.run_coroutine_threadsafe(
                self._daemon.set_quiet_mode(
                    str(kind), duration_minutes=minutes, source="overlay",
                ),
                self._daemon_loop,
            )
        except Exception:
            logger.debug("quiet_requested scheduling failed", exc_info=True)

    def request_quiet_mode(
        self,
        kind: str,
        *,
        duration_minutes: int | None = None,
        source: str = "dashboard",
    ) -> None:
        """Public entry point used by the dashboard's pause capsule
        menu, the tray menu, and the Cmd+Shift+/ shortcut. Schedules
        ``set_quiet_mode`` on the daemon loop."""
        if self._daemon is None or self._daemon_loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._daemon.set_quiet_mode(
                    str(kind),
                    duration_minutes=duration_minutes,
                    source=source,
                ),
                self._daemon_loop,
            )
        except Exception:
            logger.debug("request_quiet_mode scheduling failed", exc_info=True)

    def _desktop_is_focused_probe(self) -> bool:
        """P0 §3.12: returns True iff a Cortex window has keyboard focus.

        Phase-3 P0-6 / P1-N1: ``QApplication.focusWindow()`` and
        ``QWidget.windowHandle()`` are **not** thread-safe. The probe
        is called from the daemon's asyncio thread; touching Qt
        objects directly from there can SIGSEGV under load. Instead,
        we maintain a cached boolean updated on the Qt main thread via
        the ``focusWindowChanged`` signal (wired in ``_start_daemon``)
        and the probe just reads the atomic.
        """
        return bool(getattr(self, "_dashboard_is_focused", False))

    def _refresh_focus_cache(self, focus_window: object | None = None) -> None:
        """Slot invoked on the Qt main thread when the focused window
        changes. Walks Cortex's owned top-level windows and caches a
        boolean for ``_desktop_is_focused_probe`` to read.

        Safe to call from anywhere on the Qt thread; the daemon thread
        only ever reads the cached boolean (Python attribute access is
        atomic for refcount-1 booleans on CPython).
        """
        try:
            if focus_window is None:
                from PySide6.QtWidgets import QApplication
                app = QApplication.instance()
                # ``QApplication.instance()`` is typed to return the base
                # ``QCoreApplication`` (no ``focusWindow``); the running
                # instance is a ``QApplication``, so narrow by attribute
                # presence before reading the focused window.
                focus_fn = getattr(app, "focusWindow", None)
                if app is None or focus_fn is None:
                    self._dashboard_is_focused = False
                    return
                focus_window = focus_fn()
            if focus_window is None:
                # Backgrounded entirely — fire OS notifications.
                self._dashboard_is_focused = False
                return
            owned_widgets = (
                self._dashboard,
                getattr(self, "_settings", None),
                getattr(self, "_connections", None),
                getattr(self, "_overlay", None),
                getattr(self, "_onboarding", None),
            )
            for w in owned_widgets:
                if w is None:
                    continue
                try:
                    handle = w.windowHandle()
                except Exception:
                    handle = None
                if handle is not None and handle is focus_window:
                    self._dashboard_is_focused = True
                    return
            self._dashboard_is_focused = False
        except Exception:
            # On any error, treat as focused so OS notifications stay
            # quiet (least-surprising behaviour for a broken probe).
            self._dashboard_is_focused = True

    async def _run_break_overlay(
        self,
        duration_seconds: float,
        breathing_pattern: str,
        audio_cue: bool,
    ) -> tuple[float, bool]:
        """P0 §3.7: drive the full-screen Qt break overlay from asyncio.

        The daemon owns the asyncio loop; the Qt overlay needs the Qt
        thread. We marshal across the boundary by scheduling the
        ``BreakOverlayWindow.run`` call on the Qt main thread (via the
        Qt event loop) and awaiting its completion through a
        ``concurrent.futures.Future`` that the Qt side resolves once
        ``run`` returns.

        The contract returned to the controller — ``(elapsed_seconds,
        completed)`` — feeds the daemon's BiologyBreakController which
        in turn computes ``recovery_delta`` and persists the BreakRecord.
        """
        import concurrent.futures

        from PySide6.QtCore import QTimer as _QTimer

        future: concurrent.futures.Future[tuple[float, bool]] = (
            concurrent.futures.Future()
        )
        pattern: Literal["box", "4-7-8", "coherent"]
        if breathing_pattern in ("box", "4-7-8", "coherent"):
            pattern = breathing_pattern
        else:
            pattern = "box"

        def _on_qt_thread() -> None:
            try:
                if self._break_overlay is None:
                    self._break_overlay = BreakOverlayWindow()
                # NB: BreakOverlayWindow.run blocks on a local QEventLoop
                # so it is safe to call directly from the Qt thread.
                elapsed, completed = self._break_overlay.run(
                    duration_seconds=float(duration_seconds),
                    pattern=pattern,
                    audio_cue=bool(audio_cue),
                )
            except Exception as exc:  # pragma: no cover - exercised in manual QA
                logger.exception("BreakOverlayWindow.run failed")
                future.set_exception(exc)
                return
            future.set_result((elapsed, completed))

        # QTimer.singleShot is the canonical way to schedule a callable on
        # the Qt main thread from any thread.
        _QTimer.singleShot(0, _on_qt_thread)
        # asyncio.wrap_future bridges the concurrent.futures.Future into
        # the asyncio loop so the caller can ``await`` it.
        return await asyncio.wrap_future(future)

    def _copy_to_clipboard(self, text: str) -> None:
        """Copy ``text`` to the macOS clipboard via QGuiApplication.

        Falls back to a debug log if the clipboard is unavailable (test
        fixtures, headless CI).
        """
        if not text:
            return
        try:
            from PySide6.QtGui import QGuiApplication

            clip = QGuiApplication.clipboard()
            if clip is not None:
                clip.setText(text)
                logger.info(
                    "Copied %d chars to clipboard via desktop overlay", len(text)
                )
        except Exception:
            logger.debug("Clipboard copy failed", exc_info=True)

    @Slot(dict)
    def _on_settings_changed(self, settings: dict) -> None:
        if self._daemon is not None and self._daemon_loop is not None:
            asyncio.run_coroutine_threadsafe(
                self._daemon.apply_settings(settings),
                self._daemon_loop,
            )

    # ── Audit-prod P0 fix: orphan-signal handlers ──────────────────────
    #
    # Each handler matches the established ``_on_settings_changed``
    # pattern — schedule the daemon-side coroutine on the daemon's
    # asyncio loop via ``run_coroutine_threadsafe``. None of these
    # touch Qt state directly; result fan-out (e.g. TEST_PROVIDER) lands
    # via the existing bridge → dashboard/settings ``apply_*`` slots.

    @Slot(dict)
    def _on_break_pill_clicked(self, payload: dict) -> None:
        """P0 §3.7: user clicked the "Take a break" pill on the dashboard.

        Schedules ``daemon.start_biology_break`` directly so the desktop
        full-screen overlay drives the same breathing controller as a
        daemon-initiated promotion. Pulls duration / pattern / audio cue
        out of the cached BREAK_RECOMMENDATION payload that the dashboard
        echoes back; falls back to the controller's existing defaults
        when fields are missing.
        """
        if self._daemon is None or self._daemon_loop is None:
            return
        if not isinstance(payload, dict):
            payload = {}
        # The payload mirrors MessageType.BREAK_RECOMMENDATION fields.
        # ``duration_seconds`` defaults to 240 (4 min) per the spec.
        try:
            duration_seconds = int(payload.get("duration_seconds") or 240)
        except (TypeError, ValueError):
            duration_seconds = 240
        pattern_raw = payload.get("breathing_pattern")
        pattern: str | None = (
            str(pattern_raw) if isinstance(pattern_raw, str) and pattern_raw else None
        )
        audio_cue = bool(payload.get("audio_cue", True))
        reason = str(payload.get("reason") or "user_break_pill_click")[:120]
        try:
            asyncio.run_coroutine_threadsafe(
                self._daemon.start_biology_break(
                    intervention_id=None,
                    duration_seconds=duration_seconds,
                    breathing_pattern=pattern,
                    audio_cue=audio_cue,
                    reason=reason,
                ),
                self._daemon_loop,
            )
        except Exception:
            logger.debug(
                "start_biology_break scheduling failed", exc_info=True,
            )

    @Slot(str)
    def _on_undo_action_requested(self, intervention_id: str) -> None:
        """Dashboard Undo toast / "Restore previous state" pill click.

        Submits an ``undone`` USER_ACTION so the restore manager rolls
        back the most-recent reversible mutation for this intervention.
        Mirrors the action verb the helpfulness tracker already
        understands.
        """
        if (
            self._daemon is None
            or self._daemon_loop is None
            or not intervention_id
        ):
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._daemon._handle_user_action({
                    "action": "undone",
                    "intervention_id": str(intervention_id),
                }),
                self._daemon_loop,
            )
        except Exception:
            logger.debug(
                "undo USER_ACTION scheduling failed", exc_info=True,
            )

    @Slot()
    def _on_force_recap_requested(self) -> None:
        """P0 §3.21: Cmd+Shift+R developer shortcut → ``daemon.force_recap``.

        Broadcasts a SESSION_RECAP for the active session (or a
        synthesised empty recap when no session is running) so the
        dashboard's recap sheet has something to render.
        """
        if self._daemon is None or self._daemon_loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._daemon.force_recap(),
                self._daemon_loop,
            )
        except Exception:
            logger.debug(
                "force_recap scheduling failed", exc_info=True,
            )

    @Slot()
    def _on_dismiss_overlay_requested(self) -> None:
        """P0 §3.21: Cmd+Shift+D developer shortcut → dismiss every
        active overlay across surfaces and clear pending intervention
        state. Reuses the daemon's ``dismiss_active_overlay`` helper.
        """
        if self._daemon is None or self._daemon_loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._daemon.dismiss_active_overlay(),
                self._daemon_loop,
            )
        except Exception:
            logger.debug(
                "dismiss_active_overlay scheduling failed", exc_info=True,
            )

    @Slot(str)
    def _on_test_provider_requested(self, provider: str) -> None:
        """P0 §3.19: Settings → LLM backend → "Test connection".

        Schedules ``daemon.test_provider(provider)`` and routes the
        result back into ``settings.apply_provider_test_result`` so the
        status pill updates with latency / error. The future's result
        is a ``TestProviderResult`` Pydantic model — we marshal to a
        dict before hopping back onto the Qt thread.
        """
        if self._daemon is None or self._daemon_loop is None:
            return
        settings_dialog = self._settings
        if settings_dialog is None:
            return
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._daemon.test_provider(str(provider)),
                self._daemon_loop,
            )
        except Exception:
            logger.debug(
                "test_provider scheduling failed", exc_info=True,
            )
            return

        def _on_done(fut: Any) -> None:
            payload: dict[str, Any]
            try:
                result = fut.result()
            except Exception as exc:
                logger.debug("test_provider raised: %r", exc)
                payload = {
                    "provider": str(provider),
                    "ok": False,
                    "latency_ms": None,
                    "error": "exception",
                }
            else:
                if hasattr(result, "model_dump"):
                    try:
                        payload = result.model_dump(mode="json")
                    except Exception:
                        payload = {
                            "provider": str(provider),
                            "ok": False,
                            "latency_ms": None,
                            "error": "serialization_failed",
                        }
                elif isinstance(result, dict):
                    payload = result
                else:
                    payload = {
                        "provider": str(provider),
                        "ok": False,
                        "latency_ms": None,
                        "error": "bad_result_type",
                    }
            # Hop onto the Qt main thread via singleShot so the dialog's
            # paint chain stays on the correct thread.
            try:
                from PySide6.QtCore import QTimer as _QTimer

                _QTimer.singleShot(
                    0,
                    lambda p=payload: settings_dialog.apply_provider_test_result(p),
                )
            except Exception:
                logger.debug(
                    "apply_provider_test_result marshal failed", exc_info=True,
                )

        try:
            future.add_done_callback(_on_done)
        except Exception:
            logger.debug(
                "test_provider done-callback wire failed", exc_info=True,
            )

    def _show_dashboard(self) -> None:
        if self._dashboard is not None:
            self._dashboard.show()
            self._dashboard.raise_()
            self._dashboard.activateWindow()
            # Force macOS to bring the app + window to front
            try:
                from AppKit import NSApp

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
        """Run calibration in-process so it shares the daemon's webcam pipeline.

        P0 §3.4 — replaces the legacy subprocess spawn. Two paths:

        * If the daemon is running, schedule the ``CalibrationRunner``
          coroutine on ``self._daemon_loop`` via
          ``asyncio.run_coroutine_threadsafe`` so the same TCC context
          and webcam handle are reused. The daemon's capture pipeline
          is paused for the duration so the runner can own the camera
          exclusively.
        * If the daemon is not yet running (onboarding-only path), spin
          up a fresh asyncio loop on a Qt worker thread and drive the
          runner there. Camera handle is released in the runner's
          ``finally`` block regardless of outcome.
        """
        # Lazy import keeps controller imports cheap (cv2 stays out of
        # the module-level dependency graph until calibration starts).
        from cortex.services.capture_service.calibration_runner import (
            CalibrationRunner,
        )

        # Guard against re-entry: a second click on Begin while a run
        # is in flight is a no-op.
        if getattr(self, "_calibration_runner", None) is not None:
            logger.info("calibration already running; ignoring duplicate request")
            return

        def _progress_cb(progress: object) -> None:
            try:
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
                self._bridge.calibration_progress.emit(payload)
            except Exception:
                logger.debug("calibration progress emit failed", exc_info=True)

        runner = CalibrationRunner(config=self._config)
        self._calibration_runner = runner

        async def _drive() -> None:
            paused_capture = False
            try:
                if (
                    self._daemon is not None
                    and hasattr(self._daemon, "_capture_pipeline")
                    and getattr(self._daemon._capture_pipeline, "is_running", False)
                ):
                    try:
                        await self._daemon._capture_pipeline.stop()
                        paused_capture = True
                    except Exception:
                        logger.debug(
                            "pausing capture pipeline for calibration failed",
                            exc_info=True,
                        )
                await runner.start(on_progress=_progress_cb)
                await runner.finish()
            except Exception:
                logger.exception("calibration run failed")
            finally:
                self._calibration_runner = None
                if paused_capture and self._daemon is not None:
                    try:
                        await self._daemon._capture_pipeline.start()
                    except Exception:
                        logger.debug(
                            "restoring capture pipeline after calibration failed",
                            exc_info=True,
                        )

        if self._daemon_loop is not None and self._daemon_loop.is_running():
            asyncio.run_coroutine_threadsafe(_drive(), self._daemon_loop)
            return

        # Daemon not running — drive on a fresh loop in a worker thread.
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
        """Marshal calibration progress (queued from the daemon thread)
        onto the onboarding card and refresh the dashboard freshness
        pill when the run completes."""
        if self._onboarding is not None and hasattr(
            self._onboarding, "apply_calibration_progress"
        ):
            try:
                self._onboarding.apply_calibration_progress(**payload)
            except Exception:
                logger.debug("apply_calibration_progress failed", exc_info=True)
        if payload.get("status") == "completed":
            if self._dashboard is not None and hasattr(
                self._dashboard, "refresh_baseline_freshness"
            ):
                try:
                    self._dashboard.refresh_baseline_freshness()
                except Exception:
                    logger.debug("dashboard freshness refresh failed", exc_info=True)
            if self._settings is not None and hasattr(
                self._settings, "refresh_baseline_freshness"
            ):
                try:
                    self._settings.refresh_baseline_freshness()
                except Exception:
                    logger.debug("settings freshness refresh failed", exc_info=True)

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
        # P0 §3.11: route through ``set_quiet_mode`` so QUIET_MODE_STATE
        # broadcasts back to every surface (overlay capsule, popup
        # pill, VS Code status bar). The legacy local ``_paused`` flag
        # stays for ``_on_overlay_dismissed`` etc., but the daemon is
        # the source of truth now.
        kind = "pause" if self._paused else "off"
        self.request_quiet_mode(kind, source="tray")

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
        # P0 §3.11: route through the unified ``set_quiet_mode`` path so
        # the QUIET_MODE_STATE broadcast updates every surface.
        self.request_quiet_mode(
            "snooze_15", duration_minutes=15, source="tray",
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

    @Slot()
    def _on_daemon_stop_requested(self) -> None:
        """Phase 4.B fix (#2): the dashboard Stop button fired
        ``daemon_stop_requested``. Schedule ``daemon.stop()`` on the
        daemon-thread loop so the SESSION_RECAP broadcast pipeline can
        run; do NOT quit Qt here — that's :meth:`_on_gui_quit_requested`.

        The previous implementation conflated stop + quit into a single
        ``_stop_daemon_and_quit`` slot which ran the synchronous
        ``future.result(timeout=5.0)`` and then ``_quit()`` immediately,
        leaving no window for the recap sheet to render between the
        broadcast and the Qt exit. Splitting the slots fixes that.
        """
        # Re-entrancy guard: a double-click on Stop (or a stray legacy
        # ``stop_requested`` connection that fans into the same handler)
        # would otherwise schedule ``daemon.stop()`` twice. The dashboard
        # also emits ``daemon_stop_requested`` and the legacy
        # ``stop_requested`` alias for back-compat; only the canonical
        # one is wired in ``run()`` now, but the guard keeps us safe if a
        # future test harness or peer surface fires either signal again.
        if self._stopping:
            logger.debug(
                "_on_daemon_stop_requested: stop already in flight; "
                "ignoring re-entry"
            )
            return
        self._stopping = True
        logger.info("Dashboard Stop button — scheduling daemon stop")
        if (
            self._daemon is None
            or self._daemon_loop is None
            or not self._daemon_loop.is_running()
        ):
            # Daemon not running — no recap to wait for; jump straight
            # to the quit path so the user isn't stuck.
            logger.debug(
                "daemon_stop_requested: no live daemon loop; quitting directly"
            )
            self._on_gui_quit_requested()
            return

        # Fire-and-forget: the future's done-callback notifies the UI
        # that the stop resolved, but we do NOT block here on its
        # result. Blocking would prevent the recap sheet from rendering
        # (the Qt event loop needs to keep ticking so the SESSION_RECAP
        # broadcast can fan out through the wrapper into the bridge
        # signal queue).
        bridge = self._bridge

        def _on_done(future: Any) -> None:
            try:
                exc = future.exception()
            except Exception:
                exc = None
            if exc is not None:
                logger.warning(
                    "daemon.stop() raised: %r", exc, exc_info=False,
                )
            try:
                bridge.on_daemon_stopped()
            except Exception:
                logger.debug(
                    "daemon_stopped emit failed (non-fatal)", exc_info=True
                )

        try:
            future = asyncio.run_coroutine_threadsafe(
                self._daemon.stop(), self._daemon_loop,
            )
            future.add_done_callback(_on_done)
        except Exception:
            logger.exception("Failed to schedule daemon.stop()")
            # Schedule the done callback ourselves so the UI doesn't wedge
            # waiting for ``daemon_stopped``.
            try:
                bridge.on_daemon_stopped()
            except Exception:
                logger.debug(
                    "daemon_stopped fallback emit failed", exc_info=True
                )

    @Slot()
    def _on_gui_quit_requested(self) -> None:
        """Phase 4.B fix (#2): the user has consumed the recap (or the
        watchdog fired). Run the regular ``_quit`` path that closes
        windows and exits the Qt event loop. ``aboutToQuit`` will then
        fire ``_shutdown_daemon`` as a safety net — by which point the
        daemon should already have stopped via :meth:`_on_daemon_stop_requested`.
        """
        logger.info("GUI quit requested — exiting Qt event loop")
        self._quit()

    def _on_user_initiated_quit(self) -> None:
        """Phase 4.B fix (#4): single entry point for the tray Quit
        action and the macOS app menu's Quit (Cmd+Q reaches us via
        ``QApplication.lastWindowClosed`` when the dashboard is the
        only window — see ``run`` for the connection).

        If a session is active (daemon has produced or is producing a
        report) and the dashboard is available, route through the
        two-phase recap flow so the user always sees their summary.
        Otherwise fall through to a direct quit so they don't have to
        wait for a phantom watchdog.
        """
        if not self._session_active():
            logger.debug(
                "user_initiated_quit: no active session; quitting directly"
            )
            self._on_gui_quit_requested()
            return
        consumer = (
            getattr(self._dashboard, "_consumer", None)
            if self._dashboard is not None else None
        )
        if consumer is None:
            logger.debug(
                "user_initiated_quit: no consumer tab; quitting directly"
            )
            self._on_gui_quit_requested()
            return
        # If already mid-stop (double-click on Quit while the recap is
        # rendering) just let the existing flow finish.
        if getattr(consumer, "_stopping", False):
            logger.debug(
                "user_initiated_quit: stop already armed; ignoring"
            )
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
            # Fall back to a direct quit so we don't wedge the user.
            self._on_gui_quit_requested()

    def _session_active(self) -> bool:
        """Heuristic for "is there an active session report?".

        Used to decide whether Cmd+Q / tray Quit should route through
        the recap flow or quit directly. We consider the daemon active
        if its loop is running AND either we have a cached recap (a
        previous session ended cleanly) or a capture pipeline that's
        currently running (the user is mid-session).
        """
        if self._daemon is None or self._daemon_loop is None:
            return False
        if not self._daemon_loop.is_running():
            return False
        try:
            pipeline = getattr(self._daemon, "_capture_pipeline", None)
            if pipeline is not None and getattr(pipeline, "is_running", False):
                return True
        except Exception:
            logger.debug(
                "capture_pipeline.is_running probe raised", exc_info=True
            )
        try:
            if getattr(self._daemon, "_latest_session_recap", None):
                return True
        except Exception:
            logger.debug(
                "_latest_session_recap probe raised", exc_info=True
            )
        # Default to True when the daemon is alive — better to show a
        # (possibly empty) recap than to silently swallow a session.
        return True

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

        B2 (audit-prod): on success, surface a one-line confirmation
        toast on the dashboard so the user sees that the new token is
        live and the next intervention will use the LLM.
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
                    # Hop back to the Qt main thread to surface the toast.
                    try:
                        from PySide6.QtCore import QTimer

                        QTimer.singleShot(
                            0, lambda: self._show_byok_success_toast()
                        )
                    except Exception:
                        logger.debug(
                            "BYOK success toast scheduling failed",
                            exc_info=True,
                        )
                else:
                    logger.warning("LLM planner credentials reload returned False")
            except Exception:
                logger.exception("LLM planner credentials reload raised")

        try:
            self._daemon_loop.call_soon_threadsafe(_do_reload)
        except Exception:
            logger.debug("Failed to schedule reload_credentials", exc_info=True)

    def _on_settings_save_failed(self, reason: str) -> None:
        """Audit-prod fix (P1-2): surface a failed settings save in the
        dashboard toast so the user sees that their change did not stick.
        """
        if self._dashboard is None or not hasattr(self._dashboard, "show_error"):
            logger.warning("Settings save failed but no dashboard for toast: %s", reason)
            return
        try:
            self._dashboard.show_error(
                "Settings save failed",
                str(reason or "Unknown error — see daemon log."),
                "",
            )
        except Exception:
            logger.debug("show_error for settings_save_failed raised", exc_info=True)

    def _on_auth_token_rotated(self, new_token: str) -> None:
        """Audit-prod fix (P1-3): in-process daemon already reads the
        token from disk on every IDENTIFY callback path; a rotation
        therefore takes effect on the next reconnect. We surface a
        confirmation toast so the user sees that the rotation succeeded.
        """
        if self._dashboard is not None and hasattr(
            self._dashboard, "show_info_toast"
        ):
            try:
                self._dashboard.show_info_toast(
                    "Authentication token rotated",
                    "Cortex will use the new token on next reconnect.",
                )
            except Exception:
                logger.debug("show_info_toast for auth_token_rotated raised", exc_info=True)

    def _show_byok_success_toast(self) -> None:
        if self._dashboard is None or not hasattr(self._dashboard, "show_info_toast"):
            return
        try:
            self._dashboard.show_info_toast(
                "Cortex is now using your LLM",
                "BYOK token saved — your next intervention will use it.",
            )
        except Exception:
            logger.debug("show_info_toast call failed", exc_info=True)
