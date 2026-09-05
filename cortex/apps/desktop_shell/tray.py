"""Desktop Shell — System Tray Icon (macOS-native refactor).

Qt's ``QSystemTrayIcon`` is used as the cross-platform fallback (it actually
bridges to ``NSStatusItem`` on macOS under the hood). The pre-refactor icon
was a flat colored disc — a Material-flavoured dot that stood out in the
otherwise monochrome macOS menu bar. The refactor swaps that for a
heart-shaped silhouette tinted to the current state color, matching the
templated SF-Symbol aesthetic that Apple's HIG asks menu-bar apps to adopt.

All public Signals + ``update_state`` / ``set_connected`` / ``set_paused``
methods are preserved byte-identical.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from cortex.apps.desktop_shell import mac_native
from cortex.apps.desktop_shell.palette_runtime import active_state_color
from cortex.apps.desktop_shell.tokens import STATE_COLORS as _STATE_HEX
from cortex.apps.desktop_shell.tokens import STATE_LABELS

logger = logging.getLogger(__name__)


# State → QColor. Re-exposes the semantic palette through QColor so all
# tooltip/menu rendering stays consistent.
STATE_COLORS: dict[str, QColor] = {
    state: QColor(hex_value) for state, hex_value in _STATE_HEX.items()
}

DISCONNECTED_COLOR = QColor(140, 140, 140)

# Native NSStatusItem menu titles (the Qt fallback menu mirrors them).
_NATIVE_PAUSE = "Pause"
_NATIVE_RESUME = "Resume"
_NATIVE_SNOOZE = "Snooze 15 min"
_NATIVE_QUIET_SESSION = "Quiet for this session"
_NATIVE_TURN_OFF = "Turn off suggestions this session"
_QUIT_IDLE = "Quit Cortex"
_QUIT_ACTIVE = "End Session and Quit Cortex"

# F34: keep the tray Quit action disabled for this long after a stop is
# requested. Re-enables earlier on ``notify_daemon_stopped``. Mirrors the
# dashboard's safety-timer budget.
_STOP_SAFETY_TIMEOUT_MS = 10_000


def _heart_path(size: int) -> QPainterPath:
    """Return a heart-shape painter path inscribed in a `size`×`size` box."""
    path = QPainterPath()
    # Heart geometry — two arcs joined into a chevron at the bottom.
    cx = size / 2.0
    top = size * 0.18
    bottom_tip = size * 0.92
    side = size * 0.10
    # Start at bottom tip.
    path.moveTo(QPointF(cx, bottom_tip))
    # Left curve — sweep up to top-left lobe.
    path.cubicTo(
        QPointF(side, size * 0.62),
        QPointF(side, top),
        QPointF(cx, size * 0.34),
    )
    # Right curve — symmetric, back to tip.
    path.cubicTo(
        QPointF(size - side, top),
        QPointF(size - side, size * 0.62),
        QPointF(cx, bottom_tip),
    )
    path.closeSubpath()
    return path


def _make_heart_icon(color: QColor, size: int = 22) -> QIcon:
    """Return a heart-shaped monochrome icon tinted with the state color."""
    app_instance = getattr(QApplication, "instance", None)
    if callable(app_instance) and app_instance() is None:
        logger.debug("No QApplication instance available; returning empty tray icon")
        return QIcon()

    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(0, 0, 0, 0))

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(color)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawPath(_heart_path(size))
    painter.end()
    return QIcon(pixmap)


class CortexTrayIcon(QSystemTrayIcon):
    """Menu-bar icon. Public Signal surface preserved."""

    show_dashboard_requested = Signal()
    show_connections_requested = Signal()
    show_settings_requested = Signal()
    pause_requested = Signal()
    restore_requested = Signal()
    snooze_requested = Signal()
    disable_session_requested = Signal()
    quit_requested = Signal()
    # P0 §3.11: emitted when the user picks a kind from the tray's
    # quiet-mode submenu. Payload mirrors the dashboard signal
    # (kind, duration_minutes_or_zero).
    quiet_mode_requested = Signal(str, int)

    def __init__(self, app: QApplication) -> None:
        super().__init__(app)
        self._app = app
        self._state = "UNKNOWN"
        self._confidence = 0.0
        self._connected = False
        self._paused = False
        # F34: tray-side mirror of the dashboard stop-button state machine.
        self._stopping: bool = False
        # P0 §3.11: cached quiet-mode kind so the tray menu can mark
        # the active row with a checkmark when rebuilt.
        self._quiet_mode_kind: str = "off"

        self.setIcon(_make_heart_icon(DISCONNECTED_COLOR))
        self.setToolTip("Cortex — Disconnected")

        self._menu = QMenu()
        self._build_menu()
        self.setContextMenu(self._menu)

        # F34: safety timer for the tray Quit action. Re-enables the action
        # if the daemon never reports stopped (e.g. wedged shutdown), so the
        # user is not permanently stuck without a kill control.
        self._stop_safety_timer = QTimer(self)
        self._stop_safety_timer.setSingleShot(True)
        self._stop_safety_timer.setInterval(_STOP_SAFETY_TIMEOUT_MS)
        self._stop_safety_timer.timeout.connect(self._stop_safety_expired)

        self.activated.connect(self._on_activated)

        # Optional pure-AppKit status item for hosts that opt in. The Qt
        # tray icon already provides the menu surface, so we leave this
        # opt-in (called by main.py / controller.py if they want the
        # SF-Symbol heart aesthetic). It's harmless on non-mac builds.
        self._native_status: mac_native.StatusBarItem | None = None

    # ------------------------------------------------------------------
    # Native status-item (opt-in)
    # ------------------------------------------------------------------

    def install_native_status_item(self) -> None:
        """If running on macOS with pyobjc available, attach a real
        ``NSStatusItem`` so the menu bar icon is a templated SF Symbol
        (matches Apple's menu-bar look).

        On success we hide the Qt ``QSystemTrayIcon``: otherwise macOS shows
        TWO icons in the menu bar, and the Qt one only responds to right /
        double-clicks — confusing UX.
        """
        if not mac_native.is_macos() or self._native_status is not None:
            return
        try:
            status = mac_native.StatusBarItem(title="Cortex", template_symbol="heart.fill")
            # Sanity check: the AppKit bridge silently returns a no-op shell
            # if NSStatusBar.systemStatusBar() refused to allocate (rare, but
            # we shouldn't hide the Qt icon in that case).
            if getattr(status, "_item", None) is None:
                logger.debug("NSStatusItem allocation returned None; keeping Qt tray")
                return
            status.add_action(
                "Dashboard", lambda: self.show_dashboard_requested.emit(),
            )
            status.add_action(
                "Connect Extensions…",
                lambda: self.show_connections_requested.emit(),
            )
            status.add_separator()
            status.add_action(
                _NATIVE_PAUSE, lambda: self.pause_requested.emit(),
            )
            status.add_action(
                "Restore Workspace",
                lambda: self.restore_requested.emit(),
            )
            status.add_action(
                _NATIVE_SNOOZE,
                lambda: self.snooze_requested.emit(),
            )
            status.add_action(
                _NATIVE_QUIET_SESSION,
                lambda: self.quiet_mode_requested.emit("quiet_session", 0),
            )
            status.add_action(
                _NATIVE_TURN_OFF,
                lambda: self.disable_session_requested.emit(),
            )
            status.add_separator()
            status.add_action(
                "Settings…", lambda: self.show_settings_requested.emit(),
                key=",",
            )
            status.add_separator()
            status.add_action(
                self._quit_label(), self._handle_quit_triggered, key="q",
            )
            self._native_status = status
            self._native_quit_title = self._quit_label()
            self._native_pause_title = _NATIVE_PAUSE
            self._sync_native_checkmarks()
            # AppKit owns the menu-bar slot now — hide the Qt fallback so
            # we don't double up.
            try:
                self.setVisible(False)
            except Exception:
                pass
        except Exception:
            logger.debug("native status item install failed", exc_info=True)

    # ------------------------------------------------------------------
    # Menu (Qt fallback — runs on all platforms)
    # ------------------------------------------------------------------

    def _quit_label(self) -> str:
        """The quit item names its consequence while a session is live."""
        return _QUIT_ACTIVE if self._connected else _QUIT_IDLE

    def _sync_native_checkmarks(self) -> None:
        """Mirror the quiet-mode / pause state onto the NSStatusItem menu."""
        status = self._native_status
        if status is None:
            return
        try:
            status.set_action_checked(
                _NATIVE_SNOOZE, self._quiet_mode_kind == "snooze_15",
            )
            status.set_action_checked(
                _NATIVE_QUIET_SESSION, self._quiet_mode_kind == "quiet_session",
            )
            current_pause = getattr(self, "_native_pause_title", _NATIVE_PAUSE)
            wanted_pause = _NATIVE_RESUME if self._paused else _NATIVE_PAUSE
            if current_pause != wanted_pause:
                status.set_action_title(current_pause, wanted_pause)
                self._native_pause_title = wanted_pause
            status.set_action_checked(wanted_pause, self._paused)
            current_quit = getattr(self, "_native_quit_title", _QUIT_IDLE)
            wanted_quit = "Stopping…" if self._stopping else self._quit_label()
            if current_quit != wanted_quit:
                status.set_action_title(current_quit, wanted_quit)
                self._native_quit_title = wanted_quit
            status.set_action_enabled(wanted_quit, not self._stopping)
        except Exception:
            logger.debug("native checkmark sync failed", exc_info=True)

    def _build_menu(self) -> None:
        self._menu.clear()
        try:
            self._menu.setToolTipsVisible(True)
        except Exception:
            pass

        self._state_action = QAction("Status: —", self._menu)
        self._state_action.setEnabled(False)
        self._menu.addAction(self._state_action)

        self._menu.addSeparator()

        dashboard_action = QAction("Dashboard", self._menu)
        dashboard_action.triggered.connect(self.show_dashboard_requested.emit)
        self._menu.addAction(dashboard_action)

        connections_action = QAction("Connect Extensions…", self._menu)
        connections_action.triggered.connect(self.show_connections_requested.emit)
        self._menu.addAction(connections_action)

        self._pause_action = QAction("Pause", self._menu)
        self._pause_action.triggered.connect(self.pause_requested.emit)
        self._menu.addAction(self._pause_action)

        restore_action = QAction("Restore Workspace", self._menu)
        restore_action.triggered.connect(self.restore_requested.emit)
        self._menu.addAction(restore_action)

        snooze_action = QAction(_NATIVE_SNOOZE, self._menu)
        snooze_action.triggered.connect(self.snooze_requested.emit)
        try:
            snooze_action.setCheckable(True)
            snooze_action.setChecked(self._quiet_mode_kind == "snooze_15")
        except Exception:
            pass
        self._menu.addAction(snooze_action)
        self._snooze_action = snooze_action

        # P0 §3.11: full quiet-mode kind set under a checkmark menu.
        quiet_session_action = QAction(_NATIVE_QUIET_SESSION, self._menu)
        try:
            quiet_session_action.setCheckable(True)
            quiet_session_action.setChecked(self._quiet_mode_kind == "quiet_session")
        except Exception:
            pass
        quiet_session_action.triggered.connect(
            lambda _checked=False: self.quiet_mode_requested.emit("quiet_session", 0),
        )
        self._menu.addAction(quiet_session_action)
        self._quiet_session_action = quiet_session_action

        disable_action = QAction(_NATIVE_TURN_OFF, self._menu)
        disable_action.triggered.connect(self.disable_session_requested.emit)
        self._menu.addAction(disable_action)

        settings_action = QAction("Settings…", self._menu)
        settings_action.setShortcut("Ctrl+,")
        settings_action.triggered.connect(self.show_settings_requested.emit)
        self._menu.addAction(settings_action)

        self._menu.addSeparator()

        quit_action = QAction(self._quit_label(), self._menu)
        quit_action.setShortcut("Ctrl+Q")
        try:
            quit_action.setToolTip(
                "Stops sensing, shows the session summary, then quits Cortex."
            )
        except Exception:
            pass
        # F34: route through ``_handle_quit_triggered`` so we can disable the
        # action and swap the label to "Stopping…" on first click, coalescing
        # double-clicks to a single ``quit_requested`` emission.
        quit_action.triggered.connect(self._handle_quit_triggered)
        self._menu.addAction(quit_action)
        # Keep a handle for state transitions below.
        self._quit_action = quit_action

    # ------------------------------------------------------------------
    # State / connection updates (public API preserved)
    # ------------------------------------------------------------------

    def update_state(
        self,
        state: str,
        confidence: float,
        status: str = "estimated",
        evidence_coverage: float = 1.0,
    ) -> None:
        self._state = state
        self._confidence = confidence

        if status == "warming_up":
            label = "Still gathering"
        elif status != "estimated":
            label = "Not enough evidence"
        else:
            label = STATE_LABELS.get(state, "Status unavailable")

        # Tint only when there is a real estimate — colour must never
        # claim a state the evidence does not support.
        if status == "estimated" and state in STATE_LABELS and state != "UNKNOWN":
            color = QColor(active_state_color(state))
        else:
            color = DISCONNECTED_COLOR
        self.setIcon(_make_heart_icon(color))
        if self._native_status is not None:
            self._native_status.set_state_tint(
                color.name() if color is not DISCONNECTED_COLOR else None
            )

        tooltip = (
            f"Cortex — {label} · {confidence:.0%} evidence strength · "
            f"{evidence_coverage:.0%} coverage"
        )
        if self._paused:
            tooltip += " · Paused"
        self.setToolTip(tooltip)

        self._state_action.setText(f"Status: {label}")

    def set_connected(self, connected: bool) -> None:
        self._connected = connected
        if not connected:
            self.setIcon(_make_heart_icon(DISCONNECTED_COLOR))
            if self._native_status is not None:
                self._native_status.set_state_tint(None)
            self.setToolTip("Cortex — Disconnected")
            self._state_action.setText("Status: Disconnected")
        self._refresh_quit_label()

    def set_starting(self) -> None:
        """Show lifecycle startup without claiming a live daemon."""

        self._connected = False
        self.setIcon(_make_heart_icon(DISCONNECTED_COLOR))
        if self._native_status is not None:
            self._native_status.set_state_tint(None)
        self.setToolTip("Cortex — Starting…")
        self._state_action.setText("Status: Starting…")
        self._refresh_quit_label()

    def _refresh_quit_label(self) -> None:
        if self._stopping:
            return
        try:
            self._quit_action.setText(self._quit_label())
        except RuntimeError:
            pass
        self._sync_native_checkmarks()

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        self._pause_action.setText(_NATIVE_RESUME if paused else _NATIVE_PAUSE)
        if paused:
            label = STATE_LABELS.get(self._state, "Status unavailable")
            self.setToolTip(f"Cortex — {label} · Paused")
        # Pause is one of the quiet-mode kinds — keep the menu in sync.
        try:
            self._pause_action.setCheckable(True)
            self._pause_action.setChecked(paused)
        except Exception:
            pass
        self._sync_native_checkmarks()

    def set_quiet_mode_kind(self, kind: str) -> None:
        """P0 §3.11: surface the active quiet-mode kind in the menu so
        the user sees a checkmark next to the matching item.
        Accepts ``"off"`` / ``"snooze_15"`` / ``"quiet_session"`` /
        ``"pause"``.
        """
        self._quiet_mode_kind = kind if kind in (
            "off", "snooze_15", "quiet_session", "pause",
        ) else "off"
        for attr, match in (
            ("_snooze_action", "snooze_15"),
            ("_quiet_session_action", "quiet_session"),
        ):
            action = getattr(self, attr, None)
            if action is None:
                continue
            try:
                action.setCheckable(True)
                action.setChecked(self._quiet_mode_kind == match)
            except Exception:
                continue
        # The pause action's checkmark mirrors set_paused; force-sync.
        try:
            self._pause_action.setCheckable(True)
            self._pause_action.setChecked(self._quiet_mode_kind == "pause")
        except Exception:
            pass
        self._sync_native_checkmarks()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_dashboard_requested.emit()

    # ------------------------------------------------------------------
    # F34 — Stop / Quit state machine
    # ------------------------------------------------------------------

    def _handle_quit_triggered(self) -> None:
        """Disable the Quit action, swap label to "Stopping…", emit exactly
        one ``quit_requested``. Double-trigger coalesces because the second
        click hits a disabled action (and the ``_stopping`` guard also
        rejects it explicitly if Qt still fires the signal)."""
        if self._stopping:
            return
        self._stopping = True
        try:
            self._quit_action.setEnabled(False)
            self._quit_action.setText("Stopping…")
        except RuntimeError:
            # Action torn down; safe to ignore.
            pass
        self._sync_native_checkmarks()
        self._stop_safety_timer.start()
        self.quit_requested.emit()

    def _stop_safety_expired(self) -> None:
        """If the daemon never reports stopped, re-enable so the user can
        retry the kill rather than being stuck."""
        logger.warning(
            "Tray Quit safety timeout fired; re-enabling without daemon ack"
        )
        self.notify_daemon_stopped()

    def notify_daemon_stopped(self) -> None:
        """Called when the daemon confirms shutdown (controller wires this).
        Idempotent."""
        self._stop_safety_timer.stop()
        self._stopping = False
        try:
            self._quit_action.setEnabled(True)
            self._quit_action.setText(self._quit_label())
        except RuntimeError:
            pass
        self._sync_native_checkmarks()

    def set_stop_safety_timeout_ms(self, ms: int) -> None:
        """Allow tests to shorten the safety-timer budget."""
        self._stop_safety_timer.setInterval(int(ms))
