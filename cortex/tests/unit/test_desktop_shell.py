"""
Tests for Desktop Shell — PySide6 Control Panel & Overlay

Tests use mock PySide6 modules to verify logic without requiring
the actual Qt framework to be installed.
"""

# ruff: noqa: E402, I001

from __future__ import annotations

import json
import sys
import types

import pytest


# ---------------------------------------------------------------------------
# Mock PySide6 modules so we can import desktop_shell without Qt installed
# ---------------------------------------------------------------------------

def _setup_pyside6_mocks() -> bool:
    """Create mock PySide6 modules in sys.modules."""
    for name in list(sys.modules):
        if name == "PySide6" or name.startswith("PySide6."):
            del sys.modules[name]

    # Create module stubs
    pyside6 = types.ModuleType("PySide6")
    qtcore = types.ModuleType("PySide6.QtCore")
    qtgui = types.ModuleType("PySide6.QtGui")
    qtwidgets = types.ModuleType("PySide6.QtWidgets")

    # --- QtCore mocks ---
    class MockSignal:
        def __init__(self, *args):
            self._slots = []
        def connect(self, slot):
            self._slots.append(slot)
        def emit(self, *args):
            for slot in self._slots:
                slot(*args)
        def disconnect(self, *args):
            pass

    class MockSlot:
        def __init__(self, *args):
            pass
        def __call__(self, func):
            return func

    class MockQt:
        class AlignmentFlag:
            AlignCenter = 0x84
            AlignTop = 0x20
        class Orientation:
            Horizontal = 1
        class WindowType:
            FramelessWindowHint = 0x800
            WindowStaysOnTopHint = 0x40000
            Tool = 0x800
        class WidgetAttribute:
            WA_TranslucentBackground = 120
        class PenStyle:
            NoPen = 0
        class Key:
            Key_Escape = 0x01000000

    class MockQTimer:
        def __init__(self, parent=None):
            self._interval = 0
            self._single_shot = False
            self.timeout = MockSignal()
        def setInterval(self, ms): self._interval = ms
        def setSingleShot(self, v): self._single_shot = v
        def start(self, *args): pass
        def stop(self): pass

    class MockQRect:
        def __init__(self, *args): pass

    class MockQPropertyAnimation:
        def __init__(self, *args): pass

    class MockQObject:
        def __init__(self, *args, **kwargs): pass

    qtcore.Signal = MockSignal
    qtcore.Slot = MockSlot
    qtcore.Qt = MockQt
    qtcore.QTimer = MockQTimer
    qtcore.QObject = MockQObject
    qtcore.QRect = MockQRect
    qtcore.QPropertyAnimation = MockQPropertyAnimation
    qtcore.QEvent = type("QEvent", (), {})

    # --- QtGui mocks ---
    class MockQColor:
        def __init__(self, *args):
            self._r = args[0] if args else 0
            self._g = args[1] if len(args) > 1 else 0
            self._b = args[2] if len(args) > 2 else 0
            self._a = args[3] if len(args) > 3 else 255
        def red(self): return self._r
        def green(self): return self._g
        def blue(self): return self._b
        def alpha(self): return self._a
        def darker(self, f=200): return MockQColor(self._r, self._g, self._b)
        def name(self): return f"#{self._r:02x}{self._g:02x}{self._b:02x}"

    class MockQFont:
        class Weight:
            Bold = 75
        def __init__(self, *args): pass
        def setPointSize(self, s): pass
        def setBold(self, b): pass

    class MockQPainter:
        class RenderHint:
            Antialiasing = 1
        def __init__(self, *args): pass
        def setRenderHint(self, h): pass
        def fillRect(self, *args): pass
        def setPen(self, *args): pass
        def setBrush(self, *args): pass
        def drawEllipse(self, *args): pass
        def drawLine(self, *args): pass
        def drawText(self, *args): pass
        def setFont(self, f): pass
        def end(self): pass

    class MockQPen:
        def __init__(self, *args): pass

    class MockQPixmap:
        def __init__(self, *args): pass
        def fill(self, c): pass

    class MockQIcon:
        def __init__(self, *args): pass

    class MockQAction:
        def __init__(self, *args):
            self.triggered = MockSignal()
            self._text = args[0] if args else ""
            self._enabled = True
        def setEnabled(self, e): self._enabled = e
        def setText(self, t): self._text = t

    qtgui.QColor = MockQColor
    qtgui.QFont = MockQFont
    qtgui.QPainter = MockQPainter
    qtgui.QPen = MockQPen
    qtgui.QPixmap = MockQPixmap
    qtgui.QIcon = MockQIcon
    qtgui.QAction = MockQAction
    qtgui.QGraphicsOpacityEffect = type("QGraphicsOpacityEffect", (), {"__init__": lambda self, *a: None})

    # --- QtWidgets mocks ---
    class MockQWidget:
        def __init__(self, parent=None):
            self._visible = False
            self._size = (400, 300)
        def setWindowFlags(self, f): pass
        def setAttribute(self, a): pass
        def setMinimumSize(self, w, h): self._size = (w, h)
        def setMinimumHeight(self, h): pass
        def setMinimumWidth(self, w): pass
        def setFixedSize(self, w, h): self._size = (w, h)
        def setFixedWidth(self, w): pass
        def setFixedHeight(self, h): pass
        def show(self): self._visible = True
        def hide(self): self._visible = False
        def close(self): self._visible = False
        def raise_(self): pass
        def activateWindow(self): pass
        def isVisible(self): return self._visible
        def setWindowTitle(self, t): pass
        def resize(self, w, h): self._size = (w, h)
        def move(self, x, y): pass
        def width(self): return self._size[0]
        def height(self): return self._size[1]
        def rect(self): return MockQRect()
        def screen(self): return None
        def update(self): pass
        def paintEvent(self, e): pass
        def keyPressEvent(self, e): pass
        def setStyleSheet(self, s): pass
        def setFont(self, f): pass
        def deleteLater(self): pass

    class MockQApplication:
        def __init__(self, *args): pass
        def setApplicationName(self, n): pass
        def setOrganizationName(self, n): pass
        def setQuitOnLastWindowClosed(self, v): pass
        def exec(self): return 0
        def quit(self): pass

    class MockQLabel(MockQWidget):
        def __init__(self, text="", parent=None):
            super().__init__(parent)
            self._text = text
        def setText(self, t): self._text = t
        def text(self): return self._text
        def setFont(self, f): pass
        def setWordWrap(self, w): pass
        def setAlignment(self, a): pass

    class MockQProgressBar(MockQWidget):
        def __init__(self, parent=None):
            super().__init__(parent)
            self._value = 0
        def setRange(self, lo, hi): pass
        def setValue(self, v): self._value = v
        def value(self): return self._value
        def setTextVisible(self, v): pass
        def setFormat(self, f): pass

    class MockQCheckBox(MockQWidget):
        def __init__(self, text="", parent=None):
            super().__init__(parent)
            self._text = text
            self._checked = False
        def setChecked(self, c): self._checked = c
        def isChecked(self): return self._checked
        def setFont(self, f): pass
        def text(self): return self._text

    class MockQSlider(MockQWidget):
        class TickPosition:
            TicksBelow = 2
        def __init__(self, orientation=None, parent=None):
            super().__init__(parent)
            self._value = 3
            self.valueChanged = MockSignal()
        def setRange(self, lo, hi): pass
        def setValue(self, v): self._value = v
        def value(self): return self._value
        def setTickPosition(self, p): pass
        def setTickInterval(self, i): pass

    class MockQSpinBox(MockQWidget):
        def __init__(self, parent=None):
            super().__init__(parent)
            self._value = 0
        def setRange(self, lo, hi): pass
        def setValue(self, v): self._value = v
        def value(self): return self._value
        def setSuffix(self, s): pass

    class MockQComboBox(MockQWidget):
        def __init__(self, parent=None):
            super().__init__(parent)
            self._items = []
            self._index = 0
        def addItems(self, items): self._items = list(items)
        def currentIndex(self): return self._index
        def setCurrentIndex(self, i): self._index = i

    class MockQFrame(MockQWidget):
        class Shape:
            HLine = 4
        def setFrameShape(self, s): pass

    class MockQGroupBox(MockQWidget):
        def __init__(self, title="", parent=None):
            super().__init__(parent)

    class MockQMenu:
        def __init__(self, parent=None):
            self._actions = []
        def addAction(self, a): self._actions.append(a)
        def addSeparator(self): pass
        def clear(self): self._actions.clear()

    class MockQSystemTrayIcon:
        class ActivationReason:
            DoubleClick = 2
        def __init__(self, parent=None):
            self.activated = MockSignal()
            self._icon = None
            self._tooltip = ""
            self._menu = None
            self._visible = False
        def setIcon(self, icon): self._icon = icon
        def setToolTip(self, tip): self._tooltip = tip
        def setContextMenu(self, menu): self._menu = menu
        def show(self): self._visible = True
        def hide(self): self._visible = False

    class MockQPushButton(MockQWidget):
        def __init__(self, text="", parent=None):
            super().__init__(parent)
            self._text = text
            self.clicked = MockSignal()

    class MockLayout:
        def __init__(self, parent=None): pass
        def addWidget(self, w, *args, **kwargs): pass
        def addLayout(self, layout): pass
        def addStretch(self): pass
        def addRow(self, *args): pass
        def removeWidget(self, w): pass
        def setContentsMargins(self, *args): pass
        def setSpacing(self, s): pass

    class MockDialogButtonBox(MockQWidget):
        class StandardButton:
            Apply = 0x02000000
            Close = 0x00200000
        def __init__(self, flags=0, parent=None):
            super().__init__(parent)
            self._buttons = {}
        def button(self, flag):
            if flag not in self._buttons:
                self._buttons[flag] = MockQPushButton()
            return self._buttons[flag]

    qtwidgets.QApplication = MockQApplication
    qtwidgets.QWidget = MockQWidget
    qtwidgets.QLabel = MockQLabel
    qtwidgets.QProgressBar = MockQProgressBar
    qtwidgets.QCheckBox = MockQCheckBox
    qtwidgets.QSlider = MockQSlider
    qtwidgets.QSpinBox = MockQSpinBox
    qtwidgets.QComboBox = MockQComboBox
    qtwidgets.QFrame = MockQFrame
    qtwidgets.QGroupBox = MockQGroupBox
    qtwidgets.QMenu = MockQMenu
    qtwidgets.QSystemTrayIcon = MockQSystemTrayIcon
    qtwidgets.QPushButton = MockQPushButton
    qtwidgets.QDialogButtonBox = MockDialogButtonBox
    qtwidgets.QVBoxLayout = MockLayout
    qtwidgets.QHBoxLayout = MockLayout
    qtwidgets.QGridLayout = MockLayout
    qtwidgets.QFormLayout = MockLayout
    qtwidgets.QGraphicsOpacityEffect = type("QGraphicsOpacityEffect", (), {"__init__": lambda self, *a: None})

    sys.modules["PySide6"] = pyside6
    sys.modules["PySide6.QtCore"] = qtcore
    sys.modules["PySide6.QtGui"] = qtgui
    sys.modules["PySide6.QtWidgets"] = qtwidgets

    return True


# Install mocks before importing desktop shell modules
_mocked = _setup_pyside6_mocks()

# Now remove any cached desktop_shell imports so they pick up our mocks
_to_remove = [k for k in sys.modules if "desktop_shell" in k and k != __name__]
for k in _to_remove:
    del sys.modules[k]

from cortex.apps.desktop_shell.main import CortexApp, WebSocketBridge
from cortex.apps.desktop_shell.overlay import (
    BreathingPacer,
    OverlayWindow,
    _CYCLE_SECONDS,
    _EXHALE_SECONDS,
    _HOLD_SECONDS,
    _INHALE_SECONDS,
)
from cortex.apps.desktop_shell.settings import SettingsDialog
from cortex.apps.desktop_shell.tray import (
    STATE_COLORS,
    CortexTrayIcon,
    _make_circle_icon,
)
from cortex.apps.desktop_shell.dashboard import (
    DashboardWindow,
    HRTracePlot,
)


# ===========================================================================
# Tray Icon Tests
# ===========================================================================


class TestTrayIcon:

    def test_state_colors_defined(self):
        assert "FLOW" in STATE_COLORS
        assert "HYPER" in STATE_COLORS
        assert "HYPO" in STATE_COLORS
        assert "RECOVERY" in STATE_COLORS

    def test_make_circle_icon(self):
        from PySide6.QtGui import QColor
        icon = _make_circle_icon(QColor(255, 0, 0))
        assert icon is not None

    def test_tray_init(self):
        from PySide6.QtWidgets import QApplication
        app = QApplication()
        tray = CortexTrayIcon(app)
        assert tray._state == "FLOW"
        assert tray._confidence == 0.0
        assert not tray._connected
        assert not tray._paused

    def test_update_state(self):
        from PySide6.QtWidgets import QApplication
        tray = CortexTrayIcon(QApplication())
        tray.update_state("HYPER", 0.92)
        assert tray._state == "HYPER"
        assert tray._confidence == 0.92

    def test_set_connected(self):
        from PySide6.QtWidgets import QApplication
        tray = CortexTrayIcon(QApplication())
        tray.set_connected(True)
        assert tray._connected

    def test_set_paused(self):
        from PySide6.QtWidgets import QApplication
        tray = CortexTrayIcon(QApplication())
        tray.set_paused(True)
        assert tray._paused
        assert tray._pause_action._text == "Resume"
        tray.set_paused(False)
        assert not tray._paused
        assert tray._pause_action._text == "Pause"


# ===========================================================================
# Dashboard Tests
# ===========================================================================


class TestDashboard:

    def test_dashboard_init(self):
        dash = DashboardWindow()
        assert not dash._connected
        assert dash._timeline_events == []

    def test_update_state(self):
        dash = DashboardWindow()
        payload = {
            "state": "FLOW",
            "confidence": 0.87,
            "scores": {"flow": 0.87, "hypo": 0.05, "hyper": 0.08, "recovery": 0.0},
            "signal_quality": {"physio": 0.9, "kinematics": 0.85, "telemetry": 0.95},
            "dwell_seconds": 45.2,
            "reasons": ["Good HRV"],
        }
        dash.update_state(payload)
        assert len(dash._timeline_events) == 1
        assert dash._timeline_events[0]["state"] == "FLOW"

    def test_state_transitions_no_duplicates(self):
        dash = DashboardWindow()
        base = {"confidence": 0.8, "scores": {}, "signal_quality": {}, "dwell_seconds": 0, "reasons": []}
        dash.update_state({"state": "FLOW", **base})
        dash.update_state({"state": "FLOW", **base})
        assert len(dash._timeline_events) == 1

        dash.update_state({"state": "HYPER", **base})
        assert len(dash._timeline_events) == 2

    def test_set_connected(self):
        dash = DashboardWindow()
        dash.set_connected(True)
        assert dash._connected


class TestHRTracePlot:

    def test_add_value(self):
        plot = HRTracePlot()
        plot.add_value(72.0)
        plot.add_value(73.5)
        assert len(plot._values) == 2

    def test_max_history(self):
        plot = HRTracePlot()
        for i in range(200):
            plot.add_value(70.0 + i * 0.1)
        assert len(plot._values) == 120


# ===========================================================================
# Overlay Tests
# ===========================================================================


class TestOverlayWindow:

    def test_overlay_init(self):
        overlay = OverlayWindow()
        assert overlay._intervention_id == ""

    def test_show_intervention(self):
        overlay = OverlayWindow()
        payload = {
            "intervention_id": "int_abc123",
            "headline": "Focus on one error",
            "situation_summary": "You've been stuck for 12 minutes.",
            "primary_focus": "Fix the type error in App.tsx",
            "micro_steps": ["Look at line 67", "Check the type", "Fix it"],
            "level": "overlay_only",
            "ui_plan": {"show_overlay": True},
        }
        overlay.show_intervention(payload)
        assert overlay._intervention_id == "int_abc123"

    def test_dismiss_emits_signal(self):
        overlay = OverlayWindow()
        overlay._intervention_id = "int_xyz789"
        dismissed_ids = []
        overlay.dismissed.connect(lambda iid: dismissed_ids.append(iid))
        overlay._user_dismiss()
        assert dismissed_ids == ["int_xyz789"]

    def test_auto_dismiss(self):
        overlay = OverlayWindow()
        overlay._intervention_id = "int_timeout1"
        dismissed_ids = []
        overlay.dismissed.connect(lambda iid: dismissed_ids.append(iid))
        overlay._auto_dismiss()
        assert dismissed_ids == ["int_timeout1"]


class TestBreathingPacer:

    def test_cycle_duration(self):
        assert _INHALE_SECONDS == 4
        assert _HOLD_SECONDS == 7
        assert _EXHALE_SECONDS == 8
        assert _CYCLE_SECONDS == 19

    def test_start_stop(self):
        pacer = BreathingPacer()
        pacer.start()
        assert pacer.is_active
        pacer.stop()
        assert not pacer.is_active

    def test_phase_inhale(self):
        pacer = BreathingPacer()
        pacer._elapsed_ms = 0
        phase, remaining, scale = pacer._get_phase()
        assert phase == "Inhale"
        assert remaining == 4.0
        assert scale == pytest.approx(0.3, abs=0.01)

    def test_phase_hold(self):
        pacer = BreathingPacer()
        pacer._elapsed_ms = 5000
        phase, remaining, scale = pacer._get_phase()
        assert phase == "Hold"
        assert remaining == pytest.approx(6.0, abs=0.1)
        assert scale == 1.0

    def test_phase_exhale(self):
        pacer = BreathingPacer()
        pacer._elapsed_ms = 12000
        phase, remaining, scale = pacer._get_phase()
        assert phase == "Exhale"
        assert remaining == pytest.approx(7.0, abs=0.1)

    def test_phase_wraps(self):
        pacer = BreathingPacer()
        pacer._elapsed_ms = 19000
        phase, _, _ = pacer._get_phase()
        assert phase == "Inhale"

    def test_scale_progression(self):
        pacer = BreathingPacer()

        pacer._elapsed_ms = 0
        _, _, scale_start = pacer._get_phase()

        pacer._elapsed_ms = 3900
        _, _, scale_end = pacer._get_phase()

        pacer._elapsed_ms = 5000
        _, _, scale_hold = pacer._get_phase()

        pacer._elapsed_ms = 15000
        _, _, scale_exhale = pacer._get_phase()

        assert scale_start < scale_end
        assert scale_hold == 1.0
        assert scale_exhale < scale_hold


# ===========================================================================
# Settings Tests
# ===========================================================================


class TestSettings:

    def test_get_default_settings(self):
        settings = SettingsDialog()
        result = settings.get_settings()
        assert result["webcam_enabled"] is True
        assert result["interventions_enabled"] is True
        assert result["sensitivity"] == 3
        assert result["entry_threshold"] == pytest.approx(0.85, abs=0.01)
        assert result["cooldown_seconds"] == 60
        assert result["quiet_mode"] is False
        assert result["llm_mode"] == "azure"

    def test_sensitivity_to_threshold(self):
        settings = SettingsDialog()

        settings._sensitivity_slider._value = 1
        assert settings.get_settings()["entry_threshold"] == pytest.approx(0.95, abs=0.01)

        settings._sensitivity_slider._value = 5
        assert settings.get_settings()["entry_threshold"] == pytest.approx(0.75, abs=0.01)

    def test_llm_mode_mapping(self):
        settings = SettingsDialog()
        settings._llm_backend._index = 0
        assert settings.get_settings()["llm_mode"] == "azure"
        settings._llm_backend._index = 1
        assert settings.get_settings()["llm_mode"] == "local"
        settings._llm_backend._index = 2
        assert settings.get_settings()["llm_mode"] == "rule_based"
        settings._llm_backend._index = 3
        assert settings.get_settings()["llm_mode"] == "remote"

    def test_apply_emits_signal(self):
        settings = SettingsDialog()
        received = []
        settings.settings_changed.connect(lambda s: received.append(s))
        settings._apply_settings()
        assert len(received) == 1
        assert received[0]["sensitivity"] == 3


# ===========================================================================
# WebSocket Bridge Tests
# ===========================================================================


class TestWebSocketBridge:

    def test_bridge_init(self):
        bridge = WebSocketBridge(host="127.0.0.1", port=9473)
        assert bridge._host == "127.0.0.1"
        assert bridge._port == 9473
        assert not bridge._running

    def test_handle_state_update(self):
        bridge = WebSocketBridge()
        received = []
        bridge.state_updated.connect(lambda p: received.append(p))

        raw = json.dumps({
            "type": "STATE_UPDATE",
            "payload": {"state": "FLOW", "confidence": 0.9},
        })
        bridge._handle_message(raw)
        assert len(received) == 1
        assert received[0]["state"] == "FLOW"

    def test_handle_intervention_trigger(self):
        bridge = WebSocketBridge()
        received = []
        bridge.intervention_triggered.connect(lambda p: received.append(p))

        raw = json.dumps({
            "type": "INTERVENTION_TRIGGER",
            "payload": {"intervention_id": "int_123", "headline": "Focus"},
        })
        bridge._handle_message(raw)
        assert len(received) == 1
        assert received[0]["intervention_id"] == "int_123"

    def test_handle_invalid_json(self):
        bridge = WebSocketBridge()
        received = []
        bridge.state_updated.connect(lambda p: received.append(p))
        bridge._handle_message("not valid json {{{{")
        assert len(received) == 0

    def test_handle_unknown_type(self):
        bridge = WebSocketBridge()
        received = []
        bridge.state_updated.connect(lambda p: received.append(p))
        bridge._handle_message(json.dumps({"type": "UNKNOWN", "payload": {}}))
        assert len(received) == 0


# ===========================================================================
# CortexApp Tests
# ===========================================================================


class TestCortexApp:

    def test_app_init(self):
        app = CortexApp()
        assert not app._paused

    def test_toggle_pause(self):
        app = CortexApp()
        assert not app._paused
        app._paused = True
        assert app._paused
