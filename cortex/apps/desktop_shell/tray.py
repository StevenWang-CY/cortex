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

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from cortex.apps.desktop_shell import mac_native
from cortex.apps.desktop_shell.tokens import STATE_COLORS as _STATE_HEX

logger = logging.getLogger(__name__)


# State → QColor. Re-exposes the semantic palette through QColor so all
# tooltip/menu rendering stays consistent.
STATE_COLORS: dict[str, QColor] = {
    state: QColor(hex_value) for state, hex_value in _STATE_HEX.items()
}

DISCONNECTED_COLOR = QColor(140, 140, 140)


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

    def __init__(self, app: QApplication) -> None:
        super().__init__(app)
        self._app = app
        self._state = "FLOW"
        self._confidence = 0.0
        self._connected = False
        self._paused = False

        self.setIcon(_make_heart_icon(DISCONNECTED_COLOR))
        self.setToolTip("Cortex — Disconnected")

        self._menu = QMenu()
        self._build_menu()
        self.setContextMenu(self._menu)

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
        (matches Apple's menu-bar look)."""
        if not mac_native.is_macos() or self._native_status is not None:
            return
        try:
            status = mac_native.StatusBarItem(title="Cortex", template_symbol="heart.fill")
            status.add_action(
                "Dashboard", lambda: self.show_dashboard_requested.emit(),
            )
            status.add_action(
                "Connect Extensions…",
                lambda: self.show_connections_requested.emit(),
            )
            status.add_separator()
            status.add_action(
                "Pause", lambda: self.pause_requested.emit(),
            )
            status.add_action(
                "Restore Workspace",
                lambda: self.restore_requested.emit(),
            )
            status.add_action(
                "Snooze 15 min",
                lambda: self.snooze_requested.emit(),
            )
            status.add_action(
                "Turn Off This Session",
                lambda: self.disable_session_requested.emit(),
            )
            status.add_separator()
            status.add_action(
                "Settings…", lambda: self.show_settings_requested.emit(),
                key=",",
            )
            status.add_separator()
            status.add_action(
                "Quit Cortex", lambda: self.quit_requested.emit(), key="q",
            )
            self._native_status = status
        except Exception:
            logger.debug("native status item install failed", exc_info=True)

    # ------------------------------------------------------------------
    # Menu (Qt fallback — runs on all platforms)
    # ------------------------------------------------------------------

    def _build_menu(self) -> None:
        self._menu.clear()

        self._state_action = QAction("State: —", self._menu)
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

        snooze_action = QAction("Snooze 15 min", self._menu)
        snooze_action.triggered.connect(self.snooze_requested.emit)
        self._menu.addAction(snooze_action)

        disable_action = QAction("Turn Off This Session", self._menu)
        disable_action.triggered.connect(self.disable_session_requested.emit)
        self._menu.addAction(disable_action)

        settings_action = QAction("Settings…", self._menu)
        settings_action.setShortcut("Ctrl+,")
        settings_action.triggered.connect(self.show_settings_requested.emit)
        self._menu.addAction(settings_action)

        self._menu.addSeparator()

        quit_action = QAction("Quit Cortex", self._menu)
        quit_action.setShortcut("Ctrl+Q")
        quit_action.triggered.connect(self.quit_requested.emit)
        self._menu.addAction(quit_action)

    # ------------------------------------------------------------------
    # State / connection updates (public API preserved)
    # ------------------------------------------------------------------

    def update_state(self, state: str, confidence: float) -> None:
        self._state = state
        self._confidence = confidence

        color = STATE_COLORS.get(state, DISCONNECTED_COLOR)
        self.setIcon(_make_heart_icon(color))
        if self._native_status is not None:
            self._native_status.set_state_tint(color.name())

        tooltip = f"Cortex — {state} ({confidence:.0%})"
        if self._paused:
            tooltip += " [Paused]"
        self.setToolTip(tooltip)

        self._state_action.setText(f"State: {state} ({confidence:.0%})")

    def set_connected(self, connected: bool) -> None:
        self._connected = connected
        if not connected:
            self.setIcon(_make_heart_icon(DISCONNECTED_COLOR))
            if self._native_status is not None:
                self._native_status.set_state_tint(None)
            self.setToolTip("Cortex — Disconnected")
            self._state_action.setText("State: Disconnected")

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        self._pause_action.setText("Resume" if paused else "Pause")
        if paused:
            self.setToolTip(f"Cortex — {self._state} [Paused]")

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_dashboard_requested.emit()
