"""Reduced-motion and user-agency contracts for the full-screen break."""

from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

for _name in list(sys.modules):
    if _name == "PySide6" or _name.startswith("PySide6."):
        _module = sys.modules[_name]
        if not hasattr(_module, "__file__") or "site-packages" not in str(
            getattr(_module, "__file__", "") or ""
        ):
            del sys.modules[_name]

pytest.importorskip("PySide6")
from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from cortex.apps.desktop_shell.break_overlay import (  # noqa: E402
    _PATTERN_CYCLES,
    BreakOverlayWindow,
)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_ordinary_radius_math_preserves_breathing_cadence() -> None:
    cycle = _PATTERN_CYCLES["4-7-8"]

    inhale_phase, inhale_ratio = BreakOverlayWindow.display_radius_ratio(
        cycle,
        2.0,
        reduced_motion=False,
    )
    exhale_phase, exhale_ratio = BreakOverlayWindow.display_radius_ratio(
        cycle,
        15.0,
        reduced_motion=False,
    )

    assert inhale_phase == "Inhale"
    assert inhale_ratio == pytest.approx(0.47)
    assert exhale_phase == "Exhale"
    assert exhale_ratio == pytest.approx(0.47)


@pytest.mark.parametrize("elapsed", [0.0, 2.0, 6.0, 12.0, 18.5, 38.0])
def test_reduced_motion_keeps_fixed_radius_across_every_phase(
    elapsed: float,
) -> None:
    phase, ratio = BreakOverlayWindow.display_radius_ratio(
        _PATTERN_CYCLES["4-7-8"],
        elapsed,
        reduced_motion=True,
    )

    assert phase in {"Inhale", "Hold", "Exhale"}
    assert ratio == 0.46


def test_reduced_motion_uses_status_cadence_not_animation_cadence() -> None:
    assert BreakOverlayWindow.timer_interval_ms(False) == 33
    assert BreakOverlayWindow.timer_interval_ms(True) == 250


def test_escape_always_ends_full_screen_break(
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = BreakOverlayWindow()
    ended: list[bool] = []
    monkeypatch.setattr(window, "_end_early", lambda: ended.append(True))
    event = QKeyEvent(
        QEvent.Type.KeyPress,
        Qt.Key.Key_Escape,
        Qt.KeyboardModifier.NoModifier,
    )
    try:
        window.keyPressEvent(event)
        assert ended == [True]
    finally:
        window.deleteLater()


def test_break_overlay_exposes_accessible_exit(qapp: QApplication) -> None:
    window = BreakOverlayWindow()
    try:
        assert window.accessibleName() == "Guided breathing break"
        assert "Press Escape" in window.accessibleDescription()
    finally:
        window.deleteLater()
