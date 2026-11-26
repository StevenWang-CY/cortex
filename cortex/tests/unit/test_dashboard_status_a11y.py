"""The dashboard's live status surfaces must announce their current values.

For a label or button, Qt exposes the accessible name *in place of* the
visible text. The status pill ("Connected", "Steady activity", "Session
ended"), the health banner ("Camera offline — …"), the LLM spend chip and the
Pause capsule ("Snoozed · 15m") were all given fixed names, so VoiceOver read
"Cortex status", "Health warning", "LLM spend today" and "Pause or quiet
Cortex" no matter what they showed -- including after the session ended or
the camera went offline.
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
def window(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    _app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(mac_native, "apply_vibrancy", lambda *a, **kw: False)
    monkeypatch.setattr(mac_native, "apply_unified_titlebar", lambda *a, **kw: False)
    w = dashboard_mod.DashboardWindow()
    w._consumer.set_connected(True)
    try:
        yield w
    finally:
        w.deleteLater()


def _spoken(widget: Any) -> str:
    iface = QAccessible.queryAccessibleInterface(widget)
    assert iface is not None
    return " ".join(
        iface.text(role) for role in (QAccessible.Text.Name, QAccessible.Text.Value)
    )


def test_status_pill_announces_the_state(window: Any) -> None:
    consumer = window._consumer
    consumer.update_state({"state": "FLOW", "status": "estimated", "confidence": 0.9})
    assert consumer._state_label.text() in _spoken(consumer._state_label)

    consumer._handle_stop_clicked()
    window.notify_daemon_stopped()
    assert "Session ended" in _spoken(consumer._state_label)


def test_health_banner_announces_the_warning(window: Any) -> None:
    consumer = window._consumer
    consumer.update_state(
        {"state": "FLOW", "status": "estimated", "store": {"degraded": True}}
    )
    spoken = _spoken(consumer._health_banner)
    assert "Storage degraded" in spoken
    assert "<" not in spoken


def test_cost_chip_announces_the_amount(window: Any) -> None:
    window.apply_cost_update(0.42, 1.5)
    assert "$0.42" in _spoken(window._consumer._cost_pill)


def test_pause_capsule_announces_the_active_mode(window: Any) -> None:
    window.apply_quiet_mode_state({"kind": "snooze_15", "duration_minutes": 15})
    spoken = _spoken(window._consumer._quiet_capsule)
    assert "Snoozed" in spoken

    window.apply_quiet_mode_state({"kind": "off"})
    assert "Snoozed" not in _spoken(window._consumer._quiet_capsule)
