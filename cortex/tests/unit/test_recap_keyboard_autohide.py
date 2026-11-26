"""Keyboard use of the recap sheet must hold it open like a hover does.

The sheet closes itself after 12 s unless the pointer hovers it or clicks
it. A keyboard or VoiceOver user moving between "Quit Cortex", "View full
report" and "Close" with Tab never generates either, so the sheet -- and
the route it was about to take -- vanished under them mid-choice.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from cortex.apps.desktop_shell import recap_sheet as recap_mod


@pytest.fixture()
def sheet(monkeypatch: pytest.MonkeyPatch) -> Iterator[recap_mod.RecapSheet]:
    _app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(recap_mod, "_reduced_motion_active", lambda: True)
    widget = recap_mod.RecapSheet()
    try:
        yield widget
    finally:
        widget._autohide.stop()
        widget.hide()
        widget.deleteLater()


def test_tab_between_buttons_cancels_the_autohide(sheet: recap_mod.RecapSheet) -> None:
    sheet.show_report({"session_id": "s1", "duration_seconds": 1800})
    assert sheet._autohide.isActive()

    QTest.keyClick(sheet._close_btn, Qt.Key.Key_Backtab)

    assert not sheet._autohide.isActive()
    assert sheet.isVisible()


def test_untouched_sheet_still_autohides(sheet: recap_mod.RecapSheet) -> None:
    sheet.show_report({"session_id": "s1", "duration_seconds": 1800})

    assert sheet._autohide.isActive()
