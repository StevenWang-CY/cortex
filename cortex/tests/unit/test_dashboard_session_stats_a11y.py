"""VoiceOver must hear the "This session" numbers, not only their titles.

Each stat value label was given the accessible name "Steady" / "Longest
steady" / "Nudges shown". For a label, Qt exposes the accessible name *in
place of* its text, so assistive technology announced the three titles and
never the numbers next to them.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from PySide6.QtGui import QAccessible
from PySide6.QtWidgets import QApplication

from cortex.apps.desktop_shell import dashboard as dashboard_mod
from cortex.apps.desktop_shell import mac_native


@pytest.fixture()
def consumer(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    _app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(mac_native, "apply_vibrancy", lambda *a, **kw: False)
    monkeypatch.setattr(mac_native, "apply_unified_titlebar", lambda *a, **kw: False)
    w = dashboard_mod.DashboardWindow()
    w._consumer.set_connected(True)
    try:
        yield w._consumer
    finally:
        w.deleteLater()


def _spoken(widget: Any) -> str:
    iface = QAccessible.queryAccessibleInterface(widget)
    assert iface is not None
    return " ".join(
        iface.text(role) for role in (QAccessible.Text.Name, QAccessible.Text.Value)
    )


def test_stat_values_are_announced(consumer: Any) -> None:
    consumer.update_state({"state": "FLOW", "status": "estimated", "confidence": 0.9})
    consumer.record_intervention_seen()
    consumer.record_intervention_seen()
    consumer.record_intervention_seen()

    nudges = _spoken(consumer._today_blocked)
    assert "Nudges shown" in nudges
    assert "3" in nudges
    steady = _spoken(consumer._today_focus)
    assert "Steady" in steady
    assert consumer._today_focus.text() in steady
