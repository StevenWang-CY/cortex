"""A session-detail reply nobody is waiting for must not reopen the overlay.

``apply_session_detail`` rendered and re-showed the detail overlay for every
SESSION_DETAIL that arrived. A slow reply -- disk contention, or the 8 s
timeout already shown -- therefore popped the report back over the list
after the user had pressed Back or Escape, or over Week/Month after a
segment switch; and clicking a second session while the first reply was in
flight briefly showed the first session in place of the second.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from PySide6.QtWidgets import QApplication

from cortex.apps.desktop_shell import history_tab as ht


@pytest.fixture()
def tab() -> Iterator[ht.HistoryTab]:
    _app = QApplication.instance() or QApplication([])
    widget = ht.HistoryTab()
    try:
        yield widget
    finally:
        widget.deleteLater()


def _reply(session_id: str) -> dict[str, Any]:
    return {
        "report": {
            "session_id": session_id,
            "start_time": "2026-11-25T20:14:00Z",
            "end_time": "2026-11-25T20:44:00Z",
            "duration_seconds": 1800,
        }
    }


def test_reply_after_back_stays_closed(tab: ht.HistoryTab) -> None:
    tab._on_row_clicked("s1")
    tab._hide_detail_panel()  # Back / Escape

    tab.apply_session_detail(_reply("s1"))

    assert tab._detail_panel.isHidden()


def test_reply_for_an_abandoned_row_does_not_replace_the_pending_one(
    tab: ht.HistoryTab,
) -> None:
    tab._on_row_clicked("s1")
    tab._hide_detail_panel()
    tab._on_row_clicked("s2")

    tab.apply_session_detail(_reply("s1"))

    assert tab._detail_panel._loading, "still waiting for s2"
    assert tab._pending_detail_id == "s2"

    tab.apply_session_detail(_reply("s2"))
    assert not tab._detail_panel._loading
    assert not tab._detail_panel.isHidden()


def test_requested_reply_still_opens(tab: ht.HistoryTab) -> None:
    tab._on_row_clicked("s1")

    tab.apply_session_detail({"report": None, "error": "not_found"})

    assert not tab._detail_panel.isHidden()
    assert tab._detail_panel._error_label.text() == "We couldn't find that session on disk."


def test_recap_report_in_hand_still_opens(tab: ht.HistoryTab) -> None:
    report = _reply("s9")["report"]

    tab.open_detail("s9", report=report)

    assert not tab._detail_panel.isHidden()
    assert tab._export_btn.isEnabled()
