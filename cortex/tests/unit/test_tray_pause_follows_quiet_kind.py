"""The tray's Pause/Resume item must follow the daemon's quiet state.

Quiet mode can change from the dashboard capsule, the extension popup, the
VS Code status bar, or the daemon's own decay; the in-process host mirrors
every QUIET_MODE_STATE into ``set_quiet_mode_kind``. That moved the
checkmark but not the title or the paused flag: after a tray Pause and a
resume from anywhere else the menu still offered "Resume" (and the tooltip
still said Paused) while sensing and suggestions were running, and a pause
from the dashboard left the item reading "Pause" with a checkmark.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from PySide6.QtWidgets import QApplication

from cortex.apps.desktop_shell import tray as tray_mod


@pytest.fixture()
def tray() -> Iterator[tray_mod.CortexTrayIcon]:
    app = QApplication.instance() or QApplication([])
    icon = tray_mod.CortexTrayIcon(app)  # type: ignore[arg-type]
    icon.set_connected(True)
    icon.update_state("FLOW", 0.9, "estimated", 0.9)
    try:
        yield icon
    finally:
        icon.deleteLater()


def test_resume_elsewhere_restores_pause(tray: tray_mod.CortexTrayIcon) -> None:
    tray.set_paused(True)
    assert tray._pause_action.text() == "Resume"

    tray.set_quiet_mode_kind("off")

    assert tray._pause_action.text() == "Pause"
    assert not tray._pause_action.isChecked()
    assert "Paused" not in tray.toolTip()


def test_pause_elsewhere_offers_resume(tray: tray_mod.CortexTrayIcon) -> None:
    tray.set_quiet_mode_kind("pause")

    assert tray._pause_action.text() == "Resume"
    assert tray._pause_action.isChecked()
    assert "Paused" in tray.toolTip()


def test_snooze_is_not_a_pause(tray: tray_mod.CortexTrayIcon) -> None:
    tray.set_quiet_mode_kind("pause")

    tray.set_quiet_mode_kind("snooze_15")

    assert tray._pause_action.text() == "Pause"
    assert tray._snooze_action.isChecked()
