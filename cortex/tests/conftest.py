"""Shared pytest fixtures.

Currently exposes :func:`mock_pyside6` — a reusable helper for tests that
need to import desktop_shell modules without the heavy PySide6 dependency.
Apply via ``mock_pyside6(monkeypatch)`` from any test (transferred from the
Swift-Testing "shared fixture" pattern; swift-testing-pro / dependency
injection rule).
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest


def _make_qt_stubs() -> dict[str, types.ModuleType]:
    """Return a dict of stubbed PySide6 submodules."""
    pyside6 = types.ModuleType("PySide6")
    qtcore = types.ModuleType("PySide6.QtCore")
    qtgui = types.ModuleType("PySide6.QtGui")
    qtwidgets = types.ModuleType("PySide6.QtWidgets")

    class _Stub:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._args = args
            self._kwargs = kwargs

        def __call__(self, *args: Any, **kwargs: Any) -> _Stub:
            return _Stub(*args, **kwargs)

        def __getattr__(self, name: str) -> Any:
            return _Stub()

        def emit(self, *args: Any, **kwargs: Any) -> None:
            return None

        def connect(self, *args: Any, **kwargs: Any) -> None:
            return None

    def _make_attr(_name: str) -> type:
        class Inner(_Stub):
            pass

        return Inner

    for name in ("QObject", "QTimer", "QSettings", "Qt", "Signal", "Slot", "QRectF", "QRect", "QPointF"):
        setattr(qtcore, name, _make_attr(name))
    for name in ("QColor", "QFont", "QIcon", "QPixmap", "QPainter", "QPainterPath",
                 "QPen", "QAction"):
        setattr(qtgui, name, _make_attr(name))
    for name in (
        "QApplication", "QSystemTrayIcon", "QMenu", "QWidget", "QLabel",
        "QPushButton", "QLineEdit", "QCheckBox", "QComboBox", "QSlider",
        "QSpinBox", "QFrame", "QHBoxLayout", "QVBoxLayout", "QGridLayout",
        "QStackedWidget", "QTabWidget", "QButtonGroup", "QGraphicsDropShadowEffect",
        "QMessageBox", "QSizePolicy", "QProgressBar", "QScrollArea",
    ):
        setattr(qtwidgets, name, _make_attr(name))

    return {
        "PySide6": pyside6,
        "PySide6.QtCore": qtcore,
        "PySide6.QtGui": qtgui,
        "PySide6.QtWidgets": qtwidgets,
    }


@pytest.fixture
def mock_pyside6(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install lightweight PySide6 stubs into ``sys.modules``.

    Lets tests import :mod:`cortex.apps.desktop_shell.*` without a real Qt
    runtime. The stubs are scoped to the test function (monkeypatch undoes
    them at teardown).
    """
    stubs = _make_qt_stubs()
    for name, module in stubs.items():
        monkeypatch.setitem(sys.modules, name, module)
