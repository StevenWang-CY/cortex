"""The dashboard's "This session" numbers must start over with a new session.

The section says "This session" and its comment promises exactly what was
measured "since this session started", but its accumulators reset only after
a 30-minute gap in state updates or a calendar-day rollover. Ending a session
and choosing Start session (or, in the WebSocket host, reconnecting after a
session ended) kept the previous session's steady time, longest steady run
and "Nudges shown" on screen and kept adding to them.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
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


def _estimate(consumer: Any) -> None:
    consumer.update_state({"state": "FLOW", "status": "estimated", "confidence": 0.9})


def _end_session(window: Any) -> None:
    window._consumer._handle_stop_clicked()
    window.notify_daemon_stopped()


def test_start_session_resets_this_session_stats(window: Any) -> None:
    consumer = window._consumer
    window.set_session_restart_available(True)
    _estimate(consumer)
    consumer.record_intervention_seen()
    consumer.record_intervention_seen()
    assert consumer._today_blocked.text() == "2"
    _end_session(window)
    assert consumer._today_blocked.text() == "2", "the ended session keeps its numbers"

    consumer._on_primary_clicked()  # Start session

    assert consumer._today_blocked.text() == "0"
    assert consumer._today_best.text() == "0s"
    assert not consumer._session_stats.isVisibleTo(consumer), (
        "a new session's stats wait for its first estimate"
    )
    consumer.set_connected(True)
    _estimate(consumer)
    assert consumer._session_stats.isVisibleTo(consumer)
    assert consumer._today_blocked.text() == "0"


def test_reconnect_after_an_ended_session_resets_stats(window: Any) -> None:
    consumer = window._consumer
    window.set_session_restart_available(False)  # WebSocket host
    _estimate(consumer)
    consumer.record_intervention_seen()
    _end_session(window)

    consumer.set_connected(True)  # a new daemon session connected

    assert consumer._today_blocked.text() == "0"


def test_a_live_reconnect_keeps_the_running_session(window: Any) -> None:
    consumer = window._consumer
    _estimate(consumer)
    consumer.record_intervention_seen()

    consumer.set_connected(False)  # transient drop, session not ended
    consumer.set_connected(True)

    assert consumer._today_blocked.text() == "1"
