"""
Desktop Shell — System Tray Icon

Provides a system tray icon that:
- Shows state color (green=FLOW, red=HYPER, blue=HYPO, yellow=RECOVERY)
- Context menu: Dashboard, Pause/Resume, Settings, Quit
- Tooltip with current state and confidence
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

logger = logging.getLogger(__name__)

# State → color mapping (warm palette per design tokens)
STATE_COLORS: dict[str, QColor] = {
    "FLOW": QColor(217, 119, 87),       # Terracotta #D97757
    "HYPER": QColor(189, 73, 50),       # Dark rust #BD4932
    "HYPO": QColor(102, 98, 93),        # Warm grey #66625D
    "RECOVERY": QColor(87, 160, 217),   # Calm blue #57A0D9
}

DISCONNECTED_COLOR = QColor(158, 158, 158)  # Grey


def _make_circle_icon(color: QColor, size: int = 22) -> QIcon:
    """Create a solid circle icon of the given color."""
    app_instance = getattr(QApplication, "instance", None)
    if callable(app_instance) and app_instance() is None:
        logger.debug("No QApplication instance available; returning empty tray icon")
        return QIcon()

    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(0, 0, 0, 0))  # Transparent background

    from PySide6.QtGui import QPainter

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(color)
    painter.setPen(color.darker(120))
    margin = 1
    painter.drawEllipse(margin, margin, size - 2 * margin, size - 2 * margin)
    painter.end()

    return QIcon(pixmap)


class CortexTrayIcon(QSystemTrayIcon):
    """
    System tray icon for Cortex.

    Shows state via color, provides context menu for common actions.
    """

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

        # Initial icon
        self.setIcon(_make_circle_icon(DISCONNECTED_COLOR))
        self.setToolTip("Cortex — Disconnected")

        # Build context menu
        self._menu = QMenu()
        self._build_menu()
        self.setContextMenu(self._menu)

        # Double-click opens dashboard
        self.activated.connect(self._on_activated)

    def _build_menu(self) -> None:
        """Build the context menu."""
        self._menu.clear()

        # State display (non-clickable)
        self._state_action = QAction("State: —", self._menu)
        self._state_action.setEnabled(False)
        self._menu.addAction(self._state_action)

        self._menu.addSeparator()

        # Dashboard
        dashboard_action = QAction("Dashboard", self._menu)
        dashboard_action.triggered.connect(self.show_dashboard_requested.emit)
        self._menu.addAction(dashboard_action)

        # Connections
        connections_action = QAction("Connect Extensions\u2026", self._menu)
        connections_action.triggered.connect(self.show_connections_requested.emit)
        self._menu.addAction(connections_action)

        # Pause/Resume
        self._pause_action = QAction("Pause", self._menu)
        self._pause_action.triggered.connect(self.pause_requested.emit)
        self._menu.addAction(self._pause_action)

        restore_action = QAction("Restore Workspace", self._menu)
        restore_action.triggered.connect(self.restore_requested.emit)
        self._menu.addAction(restore_action)

        snooze_action = QAction("Snooze 15m", self._menu)
        snooze_action.triggered.connect(self.snooze_requested.emit)
        self._menu.addAction(snooze_action)

        disable_action = QAction("Turn Off This Session", self._menu)
        disable_action.triggered.connect(self.disable_session_requested.emit)
        self._menu.addAction(disable_action)

        # Settings
        settings_action = QAction("Settings", self._menu)
        settings_action.triggered.connect(self.show_settings_requested.emit)
        self._menu.addAction(settings_action)

        self._menu.addSeparator()

        # Quit
        quit_action = QAction("Quit Cortex", self._menu)
        quit_action.triggered.connect(self.quit_requested.emit)
        self._menu.addAction(quit_action)

    def update_state(self, state: str, confidence: float) -> None:
        """Update the tray icon to reflect current state."""
        self._state = state
        self._confidence = confidence

        color = STATE_COLORS.get(state, DISCONNECTED_COLOR)
        self.setIcon(_make_circle_icon(color))

        tooltip = f"Cortex — {state} ({confidence:.0%})"
        if self._paused:
            tooltip += " [Paused]"
        self.setToolTip(tooltip)

        self._state_action.setText(f"State: {state} ({confidence:.0%})")

    def set_connected(self, connected: bool) -> None:
        """Update connection status indicator."""
        self._connected = connected
        if not connected:
            self.setIcon(_make_circle_icon(DISCONNECTED_COLOR))
            self.setToolTip("Cortex — Disconnected")
            self._state_action.setText("State: Disconnected")

    def set_paused(self, paused: bool) -> None:
        """Update pause/resume state."""
        self._paused = paused
        self._pause_action.setText("Resume" if paused else "Pause")
        if paused:
            self.setToolTip(f"Cortex — {self._state} [Paused]")

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """Handle tray icon activation (double-click opens dashboard)."""
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_dashboard_requested.emit()
