"""Every Settings control without its own text needs an accessible name.

Buttons and checkboxes are announced by their text, but a combo box, spin
box or text area has none. The color-palette combo, the daily-cap spin box
and the 28 weekly-schedule combos were unnamed, so VoiceOver read "pop-up
button, on" twenty-eight times with no day or time of day, and the cap as a
bare "1.50 USD" with nothing saying what it limits.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QPlainTextEdit,
    QSlider,
    QWidget,
)

from cortex.apps.desktop_shell import mac_native
from cortex.apps.desktop_shell import settings as settings_mod

_TEXTLESS = (QComboBox, QAbstractSpinBox, QPlainTextEdit, QSlider)


@pytest.fixture()
def dialog(monkeypatch: pytest.MonkeyPatch) -> Iterator[settings_mod.SettingsDialog]:
    _app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(mac_native, "apply_vibrancy", lambda *a, **kw: False)
    monkeypatch.setattr(mac_native, "apply_unified_titlebar", lambda *a, **kw: False)
    widget = settings_mod.SettingsDialog()
    try:
        yield widget
    finally:
        widget.deleteLater()


def test_textless_controls_are_named(dialog: settings_mod.SettingsDialog) -> None:
    unnamed = [
        type(widget).__name__
        for widget in dialog.findChildren(QWidget)
        if isinstance(widget, _TEXTLESS) and not widget.accessibleName()
    ]
    assert unnamed == []


def test_schedule_slots_say_which_day_and_time(dialog: settings_mod.SettingsDialog) -> None:
    names = [
        combo.accessibleName()
        for combos in dialog._schedule_combos.values()
        for combo in combos
    ]
    assert len(names) == 28
    assert len(set(names)) == 28
    assert dialog._schedule_combos["monday"][0].accessibleName() == "Monday morning"
    assert dialog._schedule_combos["sunday"][3].accessibleName() == "Sunday evening"
