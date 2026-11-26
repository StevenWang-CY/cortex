"""History renders the user's wall clock, not the daemon's UTC stamps.

The session report generator stamps every time with ``datetime.now(UTC)``,
so the wire carries ``...Z`` / ``+00:00`` ISO strings. History formatted the
parsed value in its own offset: a session that started at 3:14 PM in New York
was listed as 8:14 PM, its detail headline and Interventions rows agreed with
that wrong clock, and the "Hourly flow" heatmap lit the UTC hour instead of
the hour the user actually worked.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator

import pytest
from PySide6.QtWidgets import QApplication

from cortex.apps.desktop_shell import history_tab as ht


@pytest.fixture()
def qapp() -> Iterator[QApplication]:
    app = QApplication.instance() or QApplication([])
    yield app  # type: ignore[misc]


def _reread_zone() -> None:
    """Apply a changed ``TZ`` where the interpreter can; libc does the rest.

    Some CPython builds have no ``time.tzset`` (the hosted Intel runner's is
    one; this fixture failed there with ``AttributeError``).  The History tab
    converts through ``datetime``, whose local conversions go through libc's
    ``localtime``, and that re-reads ``TZ`` on every call on macOS.
    """

    tzset = getattr(time, "tzset", None)
    if tzset is not None:
        tzset()


@pytest.fixture()
def utc_minus_five() -> Iterator[None]:
    """Pin the process zone to a fixed UTC-5 (POSIX sign is inverted)."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "Etc/GMT+5"
    _reread_zone()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        _reread_zone()


def test_list_and_headline_time_is_local(utc_minus_five: None) -> None:
    assert ht._format_date_time("2026-11-25T20:14:00Z") == "Wed 11/25 · 3:14 PM"
    assert ht._format_date_time("2026-11-25T20:14:00+00:00") == "Wed 11/25 · 3:14 PM"


def test_local_date_rolls_back_across_midnight(utc_minus_five: None) -> None:
    # 02:00 UTC on the 26th is still the evening of the 25th locally.
    assert ht._format_date_time("2026-11-26T02:00:00Z") == "Wed 11/25 · 9:00 PM"


def test_session_row_shows_local_start(qapp: QApplication, utc_minus_five: None) -> None:
    row = ht._SessionRow(
        {
            "session_id": "s1",
            "start_time": "2026-11-25T20:14:00Z",
            "duration_seconds": 1800,
        }
    )
    try:
        assert row._date_label.text() == "Wed 11/25 · 3:14 PM"
        assert row.accessibleName() == "Session Wed 11/25 · 3:14 PM"
    finally:
        row.deleteLater()


def test_hourly_heatmap_buckets_local_hours(qapp: QApplication, utc_minus_five: None) -> None:
    panel = ht._DetailPanel()
    try:
        panel._render_heatmap_from_transitions(
            {
                "state_transitions": [
                    {"timestamp": "2026-11-25T14:00:00Z", "from_state": "UNKNOWN", "to_state": "FLOW"},
                    {"timestamp": "2026-11-25T15:00:00Z", "from_state": "FLOW", "to_state": "HYPER"},
                ],
                "end_time": "2026-11-25T15:30:00Z",
            }
        )
        buckets = panel._heatmap._buckets
        assert buckets[9] == pytest.approx(3600.0), "flow 14:00Z is 9 AM at UTC-5"
        assert buckets[14] == 0.0, "the UTC hour must not light up"
    finally:
        panel.deleteLater()
