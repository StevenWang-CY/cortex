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

    class _QtEnumMeta(type):
        """Auto-vivify unknown ``Qt.Foo.Bar`` lookups to plain ints."""

        _counter = 0

        def __getattr__(cls, name):
            cls._counter += 1
            return cls._counter

    class _QtEnumNS:
        """Namespace whose missing attributes resolve to enum-value stubs."""

        def __init__(self, parent_name: str = "") -> None:
            self._parent_name = parent_name
            self._seen: dict[str, int] = {}

        def __getattr__(self, name):
            if name not in self._seen:
                # Stable integer per enum value so identity comparisons work.
                self._seen[name] = (hash(self._parent_name + "." + name) & 0xFFFFFFFF)
            return self._seen[name]

    class MockQt:
        AlignmentFlag = _QtEnumNS("AlignmentFlag")
        Orientation = _QtEnumNS("Orientation")
        WindowType = _QtEnumNS("WindowType")
        CursorShape = _QtEnumNS("CursorShape")
        WidgetAttribute = _QtEnumNS("WidgetAttribute")
        PenStyle = _QtEnumNS("PenStyle")
        Key = _QtEnumNS("Key")
        TextFormat = _QtEnumNS("TextFormat")
        TextInteractionFlag = _QtEnumNS("TextInteractionFlag")
        FocusPolicy = _QtEnumNS("FocusPolicy")
        FocusReason = _QtEnumNS("FocusReason")
        ShortcutContext = _QtEnumNS("ShortcutContext")
        BrushStyle = _QtEnumNS("BrushStyle")
        PenCapStyle = _QtEnumNS("PenCapStyle")
        PenJoinStyle = _QtEnumNS("PenJoinStyle")
        ScrollBarPolicy = _QtEnumNS("ScrollBarPolicy")
        ContextMenuPolicy = _QtEnumNS("ContextMenuPolicy")
        # Historical values some tests check identity on.
        class _LegacyAlignmentValues:
            AlignCenter = 0x84
            AlignTop = 0x20
            AlignVCenter = 0x80
            AlignLeft = 0x01
            AlignRight = 0x02
        # Fall back to legacy literal mapping when callers index the
        # canonical Qt5-style attribute names directly.
        # (kept for back-compat with the few tests that compare to 0x84 etc.)
        # AlignmentFlag is the modern shape; the legacy class is an alias.
        AlignmentFlag.__dict__.update(  # type: ignore[arg-type]
            {
                "AlignCenter": 0x84,
                "AlignTop": 0x20,
                "AlignVCenter": 0x80,
                "AlignLeft": 0x01,
                "AlignRight": 0x02,
            }
        )

    class MockQTimer:
        def __init__(self, parent=None):
            self._interval = 0
            self._single_shot = False
            self._active = False
            self.timeout = MockSignal()
        def setInterval(self, ms): self._interval = ms
        def setSingleShot(self, v): self._single_shot = v
        def start(self, *args): self._active = True
        def stop(self): self._active = False
        def isActive(self): return self._active

    class MockQRect:
        def __init__(self, *args): pass

    class MockQPropertyAnimation:
        def __init__(self, *args): pass

    class MockQObject:
        def __init__(self, *args, **kwargs): pass

    class MockQSettings:
        # E.2 settings.py uses QSettings("Cortex", "Desktop") for persistence.
        # In-process dict mimics the API surface the dialog touches.
        def __init__(self, *args):
            self._store: dict[str, object] = {}
        def value(self, key, default=None, _type=None):
            return self._store.get(key, default)
        def setValue(self, key, value):
            self._store[key] = value
        def sync(self):
            pass

    qtcore.Signal = MockSignal
    qtcore.Slot = MockSlot
    qtcore.Qt = MockQt
    qtcore.QTimer = MockQTimer
    qtcore.QObject = MockQObject
    qtcore.QRect = MockQRect
    qtcore.QRectF = MockQRect
    # mac_native bridge (and the new heart-shape tray icon) use QPointF.
    class MockQPointF:
        def __init__(self, x: float = 0, y: float = 0) -> None:
            self._x = x
            self._y = y
        def x(self) -> float:
            return self._x
        def y(self) -> float:
            return self._y
    qtcore.QPointF = MockQPointF
    qtcore.QPropertyAnimation = MockQPropertyAnimation
    qtcore.QSettings = MockQSettings
    qtcore.QEvent = type("QEvent", (), {})

    # F04: settings.py imports QMutex; provide a tiny mock that mimics
    # ``tryLock()`` / ``unlock()`` so SettingsDialog can be constructed
    # under the mocked Qt stack.
    class MockQMutex:
        def __init__(self) -> None:
            self._locked = False
        def tryLock(self) -> bool:
            if self._locked:
                return False
            self._locked = True
            return True
        def lock(self) -> None:
            self._locked = True
        def unlock(self) -> None:
            self._locked = False
    qtcore.QMutex = MockQMutex

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
        def drawPath(self, *args): pass
        def setFont(self, f): pass
        def end(self): pass

    class MockQPen:
        def __init__(self, *args): pass

    class MockQPainterPath:
        def __init__(self, *args): pass
        def addRoundedRect(self, *args): pass
        def moveTo(self, *args): pass
        def lineTo(self, *args): pass
        def cubicTo(self, *args): pass
        def closeSubpath(self): pass

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
        def setShortcut(self, *_args): pass

    qtgui.QColor = MockQColor
    qtgui.QFont = MockQFont
    qtgui.QPainter = MockQPainter
    qtgui.QPen = MockQPen
    qtgui.QPainterPath = MockQPainterPath
    qtgui.QPixmap = MockQPixmap
    qtgui.QIcon = MockQIcon
    qtgui.QAction = MockQAction
    qtgui.QGraphicsOpacityEffect = type("QGraphicsOpacityEffect", (), {"__init__": lambda self, *a: None})

    # --- QtWidgets mocks ---
    _SIGNAL_NAME_SUFFIXES = (
        "Activated", "Pressed", "Released", "Clicked", "Toggled",
        "Changed", "Finished", "Started", "Edited", "Selected",
        "Triggered", "Submitted", "Hovered",
    )

    class MockQWidget:
        def __init__(self, parent=None):
            self._visible = False
            self._size = (400, 300)
            self._object_name = ""
        def __getattr__(self, name):
            # Auto-vivify any attribute the mock didn't model so newly-
            # added desktop_shell code (Phase 4c additions like
            # ``QLabel.setOpenExternalLinks`` or
            # ``QLabel.linkActivated.connect``) doesn't require updating
            # every mock subclass. Names that look like Qt signals — i.e.
            # end in a common signal suffix — get a MockSignal so callers
            # can do ``foo.linkActivated.connect(...)``. Everything else
            # gets a no-op callable.
            if name.startswith("_"):
                raise AttributeError(name)
            if any(name.endswith(suffix) for suffix in _SIGNAL_NAME_SUFFIXES):
                sig = MockSignal()
                setattr(self, name, sig)
                return sig
            # Numeric / boolean accessors that callers downcast.
            _numeric_accessors = ("value", "currentIndex", "minimum", "maximum")
            _bool_accessors = ("isChecked", "isEnabled", "isVisible", "hasFocus")
            _str_accessors = ("currentText", "toolTip", "accessibleName", "accessibleDescription")
            if name in _numeric_accessors:
                return lambda *_a, **_kw: 0
            if name in _bool_accessors:
                return lambda *_a, **_kw: False
            if name in _str_accessors:
                return lambda *_a, **_kw: ""
            def _noop(*_args, **_kwargs):
                return None
            return _noop
        def setWindowFlags(self, f): pass
        def setAttribute(self, a): pass
        def setMinimumSize(self, w, h): self._size = (w, h)
        def setMinimumHeight(self, h): pass
        def setMinimumWidth(self, w): pass
        def setMaximumHeight(self, h): pass
        def setMaximumWidth(self, w): pass
        def setFixedSize(self, w, h): self._size = (w, h)
        def setFixedWidth(self, w): pass
        def setFixedHeight(self, h): pass
        def show(self): self._visible = True
        def hide(self): self._visible = False
        def close(self): self._visible = False
        def setVisible(self, v): self._visible = bool(v)
        def isHidden(self): return not self._visible
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
        def setObjectName(self, name): self._object_name = name
        def objectName(self): return self._object_name
        def setCursor(self, c): pass
        def setGraphicsEffect(self, e): pass

    class MockQApplication:
        _instance = None

        def __init__(self, *args):
            MockQApplication._instance = self
        def setApplicationName(self, n): pass
        def setOrganizationName(self, n): pass
        def setQuitOnLastWindowClosed(self, v): pass
        def exec(self): return 0
        def quit(self): pass

        @classmethod
        def instance(cls):
            return cls._instance

    class MockQLabel(MockQWidget):
        def __init__(self, text="", parent=None):
            super().__init__(parent)
            self._text = text
        def setText(self, t): self._text = t
        def text(self): return self._text
        def setFont(self, f): pass
        def setWordWrap(self, w): pass
        def setAlignment(self, a): pass

    class MockQLineEdit(MockQWidget):
        def __init__(self, text="", parent=None):
            super().__init__(parent)
            self._text = text
            # E.1: dashboard wires returnPressed → goal_set
            self.returnPressed = MockSignal()
            self.editingFinished = MockSignal()
        def setText(self, t): self._text = t
        def text(self): return self._text
        def setPlaceholderText(self, t): pass
        def setEchoMode(self, *_): pass
        class EchoMode:
            Password = 1

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
            self._checked = False
            self._enabled = True
            self.clicked = MockSignal()
        def setCheckable(self, v): pass
        def setChecked(self, v): self._checked = v
        def isChecked(self): return self._checked
        def setShortcut(self, *_args): pass
        def setAccessibleName(self, *_args): pass
        def setEnabled(self, e): self._enabled = e
        def isEnabled(self): return self._enabled
        def setText(self, t): self._text = t
        def text(self): return self._text

    class MockQTabWidget(MockQWidget):
        def __init__(self, parent=None):
            super().__init__(parent)
            self._tabs = []
        def addTab(self, widget, title):
            self._tabs.append((widget, title))

    class MockQButtonGroup:
        def __init__(self, *args, **kwargs):
            self._buttons = []
        def addButton(self, *_args, **_kwargs):
            return None
        def setExclusive(self, *_args):
            return None

    class MockQStackedWidget(MockQWidget):
        def __init__(self, parent=None):
            super().__init__(parent)
            self._children = []
            self._index = 0
        def addWidget(self, w):
            self._children.append(w)
        def setCurrentIndex(self, i):
            self._index = i

    class MockQScrollArea(MockQWidget):
        def __init__(self, parent=None):
            super().__init__(parent)
            self._widget = None
        def setWidgetResizable(self, v): pass
        def setWidget(self, w): self._widget = w

    class MockQGraphicsDropShadowEffect:
        def __init__(self, *args): pass
        def setBlurRadius(self, v): pass
        def setOffset(self, x, y): pass
        def setColor(self, c): pass

    class MockQSizePolicy:
        class Policy:
            Expanding = 0
            Preferred = 0

    class MockQMessageBox:
        @staticmethod
        def information(*args, **kwargs): return 0
        @staticmethod
        def warning(*args, **kwargs): return 0
        @staticmethod
        def critical(*args, **kwargs): return 0

    class MockLayout:
        def __init__(self, parent=None): pass
        def addWidget(self, w, *args, **kwargs): pass
        def addLayout(self, layout, *args, **kwargs): pass
        def addStretch(self, *args, **kwargs): pass
        def addSpacing(self, s): pass
        def addRow(self, *args, **kwargs): pass
        def removeWidget(self, w): pass
        def setContentsMargins(self, *args): pass
        def setSpacing(self, s): pass
        def setAlignment(self, *args): pass
        def setVerticalSpacing(self, s): pass
        def setHorizontalSpacing(self, s): pass

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
    qtwidgets.QLineEdit = MockQLineEdit
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
    qtwidgets.QTabWidget = MockQTabWidget
    qtwidgets.QButtonGroup = MockQButtonGroup
    qtwidgets.QStackedWidget = MockQStackedWidget
    qtwidgets.QScrollArea = MockQScrollArea
    qtwidgets.QSizePolicy = MockQSizePolicy
    qtwidgets.QMessageBox = MockQMessageBox
    qtwidgets.QDialogButtonBox = MockDialogButtonBox
    qtwidgets.QVBoxLayout = MockLayout
    qtwidgets.QHBoxLayout = MockLayout
    qtwidgets.QGridLayout = MockLayout
    qtwidgets.QFormLayout = MockLayout
    qtwidgets.QGraphicsOpacityEffect = type("QGraphicsOpacityEffect", (), {"__init__": lambda self, *a: None})
    qtwidgets.QGraphicsDropShadowEffect = MockQGraphicsDropShadowEffect

    # Widgets added by later Phase-4 features (Phase 4c Budget panel,
    # glossary dialog, weekly schedule, export menu, recent-goals
    # dropdown, etc.). Each maps to a permissive stub so import-time
    # ``from PySide6.QtWidgets import Q…`` resolves without forcing
    # every new widget to be hand-rolled in this mock.
    def _passthrough_stub(name: str):
        return type(name, (MockQWidget,), {"__init__": lambda self, *a, **kw: MockQWidget.__init__(self)})

    for _name in (
        "QDoubleSpinBox", "QPlainTextEdit", "QDialog", "QDialogButtonBox",
        "QFileDialog", "QShortcut", "QToolButton", "QScrollBar", "QLCDNumber",
        "QListView", "QListWidget", "QListWidgetItem", "QGraphicsView",
        "QGraphicsScene", "QToolTip", "QRadioButton", "QStyleOption",
        "QStyle", "QStyleOptionViewItem", "QAbstractItemView",
        "QSplitter", "QHeaderView", "QTreeView", "QTableView",
    ):
        if not hasattr(qtwidgets, _name):
            setattr(qtwidgets, _name, _passthrough_stub(_name))

    # ``break_overlay`` (imported transitively by ``controller.py``) uses a
    # handful of QtCore + QtMultimedia symbols not otherwise stubbed. Add
    # permissive stand-ins so the in-process controller is importable in the
    # mocked harness (needed for the audit-prod routing tests).
    class _MockQEventLoop:
        def __init__(self, *a, **kw): pass
        def exec(self, *a, **kw): return 0
        def quit(self, *a, **kw): pass
    class _MockQUrl:
        def __init__(self, *a, **kw): pass
        @staticmethod
        def fromLocalFile(p): return _MockQUrl()
    if not hasattr(qtcore, "QEventLoop"):
        qtcore.QEventLoop = _MockQEventLoop
    if not hasattr(qtcore, "QUrl"):
        qtcore.QUrl = _MockQUrl

    # Permissive module-level fallback for any QtGui symbol not explicitly
    # stubbed (e.g. QKeyEvent / QGuiApplication / QLinearGradient pulled in
    # transitively by break_overlay). Returns a passthrough widget-ish stub
    # so ``from PySide6.QtGui import Q…`` resolves under the mocked harness.
    def _qtgui_getattr(name: str):
        if name.startswith("Q"):
            return type(name, (), {"__init__": lambda self, *a, **kw: None})
        raise AttributeError(name)
    qtgui.__getattr__ = _qtgui_getattr  # type: ignore[attr-defined]

    qtmultimedia = types.ModuleType("PySide6.QtMultimedia")
    qtmultimedia.QSoundEffect = type(
        "QSoundEffect", (), {"__init__": lambda self, *a, **kw: None}
    )

    sys.modules["PySide6"] = pyside6
    sys.modules["PySide6.QtCore"] = qtcore
    sys.modules["PySide6.QtGui"] = qtgui
    sys.modules["PySide6.QtWidgets"] = qtwidgets
    sys.modules["PySide6.QtMultimedia"] = qtmultimedia

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
    _make_heart_icon,
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

    def test_make_heart_icon(self):
        from PySide6.QtGui import QColor
        icon = _make_heart_icon(QColor(255, 0, 0))
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
        # v0.2.1: default LLM mode is Bedrock (Anthropic SDK).
        assert result["llm_mode"] == "bedrock"

    def test_sensitivity_to_threshold(self):
        settings = SettingsDialog()

        settings._sensitivity_slider._value = 1
        assert settings.get_settings()["entry_threshold"] == pytest.approx(0.95, abs=0.01)

        settings._sensitivity_slider._value = 5
        assert settings.get_settings()["entry_threshold"] == pytest.approx(0.75, abs=0.01)

    def test_llm_mode_mapping(self):
        # v0.2.1: combo box order is bedrock / vertex / direct / rule_based.
        settings = SettingsDialog()
        settings._llm_backend._index = 0
        assert settings.get_settings()["llm_mode"] == "bedrock"
        settings._llm_backend._index = 1
        assert settings.get_settings()["llm_mode"] == "vertex"
        settings._llm_backend._index = 2
        assert settings.get_settings()["llm_mode"] == "direct"
        settings._llm_backend._index = 3
        assert settings.get_settings()["llm_mode"] == "rule_based"

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


# ===========================================================================
# Audit-prod: in-process controller native-action routing (Finding 1)
# ===========================================================================


import asyncio as _asyncio  # noqa: E402
import threading as _threading  # noqa: E402

from cortex.apps.desktop_shell.controller import (  # noqa: E402
    CortexAppController,
)


class _RecordingDaemon:
    """Fake daemon recording which coroutine entry points are awaited so a
    test can assert native actions never reach ``dispatch_action_to_browser``.
    """

    def __init__(self) -> None:
        self.active_intervention_id = "int_1"
        self.calls: list[tuple[str, tuple, dict]] = []

    async def start_biology_break(self, **kwargs):
        self.calls.append(("start_biology_break", (), kwargs))
        return {"ok": True}

    async def _resume_last_active_file(self, params):
        self.calls.append(("_resume_last_active_file", (params,), {}))
        return (True, None)

    async def _broadcast_prompt(self, action_type, params):
        self.calls.append(("_broadcast_prompt", (action_type, params), {}))
        return (True, None)

    async def dispatch_action_to_browser(self, intervention_id, action):
        self.calls.append(
            ("dispatch_action_to_browser", (intervention_id, action), {})
        )
        return 1

    async def _handle_user_action(self, payload):
        self.calls.append(("_handle_user_action", (payload,), {}))

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


def _run_action_invoked(action_type: str, *, extra: dict | None = None):
    """Drive ``_on_action_invoked`` against a recording daemon on a real
    asyncio loop and return the fake daemon after the scheduled coroutine
    completes."""
    controller = CortexAppController()
    daemon = _RecordingDaemon()
    controller._daemon = daemon

    loop = _asyncio.new_event_loop()
    done = _threading.Event()

    def _loop_thread():
        _asyncio.set_event_loop(loop)
        loop.run_forever()

    t = _threading.Thread(target=_loop_thread, daemon=True)
    t.start()
    controller._daemon_loop = loop

    action = {"action_type": action_type, "action_id": "a1", "label": "L"}
    if extra:
        action.update(extra)
    controller._on_action_invoked("int_1", action)

    # The slot scheduled a coroutine via run_coroutine_threadsafe; wait for
    # the loop to drain by scheduling a sentinel after it.
    def _drain():
        loop.call_soon(done.set)
    loop.call_soon_threadsafe(
        lambda: loop.call_later(0.05, _drain)
    )
    done.wait(timeout=2.0)
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=2.0)
    loop.close()
    return daemon


class TestControllerNativeActionRouting:

    def test_take_biology_break_routes_to_daemon_not_browser(self):
        daemon = _run_action_invoked(
            "take_biology_break",
            extra={"metadata": {"duration_seconds": 60, "audio_cue": False}},
        )
        names = daemon.names()
        assert "start_biology_break" in names
        assert "dispatch_action_to_browser" not in names
        # The break-break kwargs were threaded through.
        bb = next(c for c in daemon.calls if c[0] == "start_biology_break")
        assert bb[2]["duration_seconds"] == 60
        assert bb[2]["audio_cue"] is False

    def test_resume_last_active_file_routes_to_editor_adapter(self):
        daemon = _run_action_invoked("resume_last_active_file")
        names = daemon.names()
        assert "_resume_last_active_file" in names
        assert "dispatch_action_to_browser" not in names

    def test_prompt_micro_commit_is_native_log_only(self):
        daemon = _run_action_invoked(
            "prompt_micro_commit", extra={"prompt": "ship X", "text": "ship X"}
        )
        names = daemon.names()
        assert "_broadcast_prompt" in names
        assert "dispatch_action_to_browser" not in names

    def test_suggest_movement_break_is_native_log_only(self):
        daemon = _run_action_invoked(
            "suggest_movement_break", extra={"duration_seconds": 60}
        )
        names = daemon.names()
        assert "_broadcast_prompt" in names
        assert "dispatch_action_to_browser" not in names

    def test_browser_action_still_dispatches_to_browser(self):
        # A genuine browser action_type MUST reach the browser dispatch.
        assert "close_tab" in OverlayWindow._BROWSER_ACTION_TYPES
        daemon = _run_action_invoked("close_tab", extra={"tab_index": 2})
        names = daemon.names()
        assert "dispatch_action_to_browser" in names
        assert "start_biology_break" not in names

    def test_routing_sets_are_unambiguous(self):
        # Native + browser sets must be disjoint so the controller's
        # ``in _BROWSER_ACTION_TYPES`` routing decision is unambiguous.
        overlap = (
            OverlayWindow._NATIVE_ACTION_TYPES
            & OverlayWindow._BROWSER_ACTION_TYPES
        )
        assert overlap == frozenset()
        # The native action types the controller handles natively.
        for at in (
            "take_biology_break",
            "resume_last_active_file",
            "prompt_micro_commit",
            "suggest_movement_break",
        ):
            assert at not in OverlayWindow._BROWSER_ACTION_TYPES


class TestControllerOnboardingDashboard:

    def test_complete_onboarding_shows_dashboard(self, tmp_path, monkeypatch):
        import cortex.apps.desktop_shell.controller as ctrl_mod

        marker = tmp_path / "onboarding_complete"
        monkeypatch.setattr(
            ctrl_mod, "onboarding_marker_path", lambda: marker
        )
        controller = CortexAppController()
        shown = {"v": False}
        controller._show_dashboard = lambda: shown.__setitem__("v", True)
        controller._onboarding = None
        controller._complete_onboarding()
        assert shown["v"] is True
        assert marker.read_text().strip() == "completed"


# ===========================================================================
# Audit-prod: WS-mode calibration camera contention + simulation fallback
# (Finding 2) and onboarding dashboard (Finding 4)
# ===========================================================================


class _FakeBridge:
    def __init__(self) -> None:
        self.quiet_calls: list[tuple[str, str]] = []
        self.calibration_progress = MockSignalFactory()

    def send_quiet_mode_toggle(self, kind, *, source="settings_sync"):
        self.quiet_calls.append((kind, source))


class MockSignalFactory:
    def __init__(self) -> None:
        self.emitted: list = []

    def connect(self, slot):
        pass

    def emit(self, *args):
        self.emitted.append(args)


class TestWSCalibrationAndOnboarding:

    def test_ws_complete_onboarding_shows_dashboard(self, tmp_path, monkeypatch):
        import cortex.apps.desktop_shell.main as main_mod

        marker = tmp_path / "onboarding_complete"
        monkeypatch.setattr(
            main_mod, "onboarding_marker_path", lambda: marker
        )
        app = CortexApp()
        app._onboarding = None
        shown = {"v": False}
        app._show_dashboard = lambda: shown.__setitem__("v", True)
        app._complete_onboarding()
        assert shown["v"] is True

    def test_bridge_send_quiet_mode_toggle_noop_without_socket(self):
        # No loop/ws → silently no-ops (never raises).
        bridge = WebSocketBridge()
        bridge.send_quiet_mode_toggle("pause")  # must not raise

    def test_simulation_fallback_surfaces_visible_error(self):
        app = CortexApp()
        bridge = _FakeBridge()
        app._bridge = bridge
        errors: list = []

        class _Dash:
            def show_error(self, title, body, cid=""):
                errors.append((title, body))

        app._dashboard = _Dash()
        app._on_calibration_simulation_fallback()
        # A visible error was raised AND a failed-status progress emitted.
        assert len(errors) == 1
        assert "camera" in errors[0][0].lower()
        assert bridge.calibration_progress.emitted
        payload = bridge.calibration_progress.emitted[0][0]
        assert payload["status"] == "failed"


# ===========================================================================
# Audit-prod: mac_native honesty + lazy AppKit (Findings 3, 5, 6)
# ===========================================================================


class TestMacNative:

    def test_apply_vibrancy_returns_false_off_mac(self, monkeypatch):
        from cortex.apps.desktop_shell import mac_native

        # Off-mac (or AppKit unavailable) the tint cannot be applied, so the
        # honest return is False — callers must not believe vibrancy applied.
        monkeypatch.setattr(mac_native, "is_macos", lambda: False)
        monkeypatch.setattr(mac_native, "_appkit_cache", {})
        assert mac_native.apply_vibrancy(object()) is False

    def test_menu_action_target_is_lazy(self, monkeypatch):
        from cortex.apps.desktop_shell import mac_native

        # Finding 6: no eager module-level AppKit-backed class; a lazy
        # getter exists instead.
        assert not hasattr(mac_native, "_MenuActionTarget")
        assert hasattr(mac_native, "_menu_action_target_class")
        # Off-mac (forced) the lazy build returns None WITHOUT importing
        # AppKit. Clear the AppKit + memoised target caches first so the
        # forced-non-mac path is exercised deterministically on any host.
        monkeypatch.setattr(mac_native, "is_macos", lambda: False)
        monkeypatch.setattr(mac_native, "_appkit_cache", {})
        assert mac_native._menu_action_target_class() is None

    def test_install_appearance_observer_off_mac_returns_none(self, monkeypatch):
        from cortex.apps.desktop_shell import mac_native

        monkeypatch.setattr(mac_native, "is_macos", lambda: False)
        monkeypatch.setattr(mac_native, "_appkit_cache", {})
        assert mac_native.install_appearance_observer(lambda _d: None) is None

    def test_appearance_observer_exported(self):
        from cortex.apps.desktop_shell import mac_native

        assert "install_appearance_observer" in mac_native.__all__


# ===========================================================================
# Audit-prod: onboarding notification auth honesty (Finding 7)
# ===========================================================================


class _StubNotifBtn:
    def __init__(self) -> None:
        self.text = ""
        self.enabled = True

    def setText(self, t):
        self.text = t

    def setEnabled(self, v):
        self.enabled = v


class TestOnboardingNotificationAuth:

    def _make_window(self):
        from cortex.apps.desktop_shell.onboarding import OnboardingWindow

        win = OnboardingWindow.__new__(OnboardingWindow)
        win._notif_btn_ref = _StubNotifBtn()
        completed: list[str] = []
        incompleted: list[str] = []
        win.mark_step_complete = lambda s: completed.append(s)
        win.mark_step_incomplete = lambda s: incompleted.append(s)
        return win, completed, incompleted

    def test_denied_does_not_mark_complete(self):
        win, completed, incompleted = self._make_window()
        win._apply_notification_auth_result(False)
        assert "macos_notifications" not in completed
        assert "macos_notifications" in incompleted
        assert "retry" in win._notif_btn_ref.text.lower()
        assert win._notif_btn_ref.enabled is True

    def test_granted_marks_complete(self):
        win, completed, incompleted = self._make_window()
        win._apply_notification_auth_result(True)
        assert "macos_notifications" in completed
        assert "✓" in win._notif_btn_ref.text
        assert win._notif_btn_ref.enabled is False

    def test_recheck_downgrades_on_resolved_deny(self, monkeypatch):
        from cortex.libs.utils import macos_notifications as mn

        win, completed, incompleted = self._make_window()
        monkeypatch.setitem(mn._auth_state, "granted", False)
        win._recheck_notification_auth()
        assert "macos_notifications" in incompleted

    def test_request_notifications_does_not_complete_when_denied(
        self, monkeypatch
    ):
        # End-to-end: a denied/unavailable send must NOT mark the step
        # complete (pre-fix it did so unconditionally).
        from cortex.libs.utils import macos_notifications as mn

        monkeypatch.setattr(
            mn, "send_intervention_notification", lambda **kw: False
        )
        win, completed, incompleted = self._make_window()
        win._on_request_notifications()
        assert "macos_notifications" not in completed
        assert "macos_notifications" in incompleted


# ===========================================================================
# Audit-prod: connections honest verify affordance (Finding 8)
# ===========================================================================


class TestConnectionsVerify:

    def _panel(self):
        from cortex.apps.desktop_shell.connections import ConnectionsPanel

        return ConnectionsPanel.__new__(ConnectionsPanel)

    def test_manifest_check_uses_launcher_host_name(self, tmp_path, monkeypatch):
        from cortex.apps.desktop_shell import connections as conn

        monkeypatch.setattr(conn.Path, "home", staticmethod(lambda: tmp_path))
        panel = self._panel()
        # No manifest present → False.
        assert panel._native_host_manifest_installed("Chrome") is False
        # Create the canonical manifest where the installer writes it.
        manifest = (
            tmp_path / "Library" / "Application Support" / "Google" / "Chrome"
            / "NativeMessagingHosts" / "com.cortex.launcher.json"
        )
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text("{}")
        assert panel._native_host_manifest_installed("Chrome") is True

    def test_daemon_reachable_false_on_closed_port(self):
        panel = self._panel()
        # Port 1 is reserved/unbound → not reachable, returns False fast.
        assert panel._daemon_reachable(port=1) is False

    def test_verify_reports_each_prerequisite(self, monkeypatch):
        from cortex.apps.desktop_shell import connections as conn

        panel = self._panel()
        monkeypatch.setattr(
            panel, "_native_host_manifest_installed", lambda name: False
        )
        monkeypatch.setattr(panel, "_daemon_reachable", lambda: False)
        warnings: list = []
        monkeypatch.setattr(
            conn.QMessageBox, "warning",
            staticmethod(lambda *a, **kw: warnings.append(a)),
        )
        monkeypatch.setattr(
            conn.QMessageBox, "information",
            staticmethod(lambda *a, **kw: warnings.append(("info", a))),
        )
        panel._verify_browser_connection("Chrome")
        # Honest path: a warning (not a success "information") was shown
        # because neither prerequisite is satisfied.
        assert warnings
        joined = " ".join(str(x) for x in warnings)
        assert "NOT installed" in joined or "not reachable" in joined


# ===========================================================================
# Audit-prod: structured logging at startup (Finding 10 / C6)
# ===========================================================================


class TestLoggingStartup:

    def test_main_uses_configure_logging(self, monkeypatch):
        import cortex.apps.desktop_shell.main as main_mod

        called = {"v": False}
        monkeypatch.setattr(
            main_mod, "configure_logging",
            lambda **kw: called.__setitem__("v", True),
        )
        # Stop after logging is configured by raising in the branch.
        monkeypatch.setattr(main_mod.sys, "argv", ["main"])

        class _StopHere(Exception):
            pass

        def _boom():
            raise _StopHere()

        monkeypatch.setattr(main_mod, "CortexApp", lambda: type(
            "X", (), {"run": lambda self: (_ for _ in ()).throw(_StopHere())}
        )())
        try:
            main_mod.main()
        except _StopHere:
            pass
        except SystemExit:
            pass
        assert called["v"] is True

    def test_configure_logging_importable_from_main(self):
        import cortex.apps.desktop_shell.main as main_mod

        assert hasattr(main_mod, "configure_logging")
