"""A failed History detail must not render a report body for that session.

On an error reply the detail panel showed "Session unavailable" and the
error, but the report body stayed visible with every section reset to its
empty state, so a session that could not be loaded was described as "No
interventions in this session — focus held steady." The body is hidden on
error and shown again for the next report that does load.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from PySide6.QtWidgets import QApplication

from cortex.apps.desktop_shell import history_tab as ht


@pytest.fixture()
def panel() -> Iterator[ht._DetailPanel]:
    _app = QApplication.instance() or QApplication([])
    widget = ht._DetailPanel()
    try:
        yield widget
    finally:
        widget.deleteLater()


def _ok(session_id: str) -> dict[str, object]:
    return {
        "report": {
            "session_id": session_id,
            "start_time": "2026-11-25T20:00:00Z",
            "end_time": "2026-11-25T21:00:00Z",
            "duration_seconds": 3600,
        }
    }


def test_error_hides_the_report_body(panel: ht._DetailPanel) -> None:
    panel.apply_payload({"report": None, "error": "not_found"})

    assert panel._error_label.isVisibleTo(panel)
    assert not panel._scroll_widget.isVisibleTo(panel)


def test_timeout_after_loading_hides_the_body(panel: ht._DetailPanel) -> None:
    panel.set_loading(True)

    panel.apply_payload({"report": None, "error": "Request timed out"})

    assert not panel._scroll_widget.isVisibleTo(panel)


def test_a_later_report_shows_the_body_again(panel: ht._DetailPanel) -> None:
    panel.apply_payload({"report": None, "error": "unreadable"})

    panel.apply_payload(_ok("s2"))

    assert not panel._error_label.isVisibleTo(panel)
    assert panel._scroll_widget.isVisibleTo(panel)


def test_the_same_error_twice_still_reads_as_an_error(panel: ht._DetailPanel) -> None:
    """Two rows timing out give byte-identical payloads; the second must
    not be skipped as "already on screen" after loading blanked it."""
    timeout = {"report": None, "error": "Request timed out"}
    panel.set_loading(True)
    panel.apply_payload(dict(timeout))

    panel.set_loading(True)
    panel.apply_payload(dict(timeout))

    assert panel._headline.text() == "Session unavailable"
    assert panel._error_label.isVisibleTo(panel)
    assert not panel._scroll_widget.isVisibleTo(panel)
