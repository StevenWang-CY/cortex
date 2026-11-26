"""Pausing must not make the menu-bar tooltip claim a state it withheld.

``update_state`` shows "Not enough evidence" / "Still gathering" unless the
estimate is real, so the tooltip never names a state the evidence does not
support. ``set_paused(True)`` then rebuilt the tooltip from the raw state's
label: pausing during an insufficient-evidence HYPER read "Cortex — Support
may help · Paused", and pausing after the session ended read "Still
gathering · Paused" instead of Disconnected. Resuming left "· Paused" on
the tooltip until the next state update.
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
    try:
        yield icon
    finally:
        icon.deleteLater()


def test_pause_keeps_the_withheld_label(tray: tray_mod.CortexTrayIcon) -> None:
    tray.set_connected(True)
    tray.update_state("HYPER", 0.9, "insufficient_evidence", 0.2)

    tray.set_paused(True)

    tip = tray.toolTip()
    assert "Support may help" not in tip
    assert "Not enough evidence" in tip
    assert "Paused" in tip


def test_pause_while_disconnected_says_disconnected(tray: tray_mod.CortexTrayIcon) -> None:
    tray.set_connected(False)

    tray.set_paused(True)

    tip = tray.toolTip()
    assert "Disconnected" in tip
    assert "Still gathering" not in tip


def test_resume_drops_paused_from_the_tooltip(tray: tray_mod.CortexTrayIcon) -> None:
    tray.set_connected(True)
    tray.update_state("FLOW", 0.9, "estimated", 0.9)
    tray.set_paused(True)

    tray.set_paused(False)

    assert "Paused" not in tray.toolTip()
    assert "Steady activity" in tray.toolTip()
