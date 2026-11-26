"""History's hourly heatmap stays on the user's clock across a DST change.

Follow-up to 4f1300d. The bucketing loop kept the UTC offset of a FLOW
interval's start for the whole interval, so a span crossing a daylight-saving
change was filed an hour off after the switch. The daemon's own longitudinal
roll-up steps in UTC and buckets each slice by its own local hour; the
heatmap now does the same. Naive stamps are read as UTC, as elsewhere in the
History tab.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator

import pytest
from PySide6.QtWidgets import QApplication

from cortex.apps.desktop_shell import history_tab as ht


def _reread_zone() -> None:
    """Apply a changed ``TZ`` where the interpreter can; libc does the rest.

    Some CPython builds have no ``time.tzset`` (the hosted Intel runner's is
    one; these fixtures failed there with ``AttributeError``).  The History
    tab converts through ``datetime``, whose local conversions go through
    libc's ``localtime``, and that re-reads ``TZ`` on every call on macOS.
    """

    tzset = getattr(time, "tzset", None)
    if tzset is not None:
        tzset()


def _pin_zone(zone: str) -> Iterator[None]:
    previous = os.environ.get("TZ")
    os.environ["TZ"] = zone
    _reread_zone()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        _reread_zone()


@pytest.fixture()
def new_york() -> Iterator[None]:
    yield from _pin_zone("America/New_York")


@pytest.fixture()
def utc_minus_five() -> Iterator[None]:
    yield from _pin_zone("Etc/GMT+5")


def test_spring_forward_flow_lands_in_the_right_local_hours(new_york: None) -> None:
    _app = QApplication.instance() or QApplication([])
    panel = ht._DetailPanel()
    try:
        # 06:30Z = 1:30 EST; clocks jump 2:00 EST -> 3:00 EDT at 07:00Z.
        panel._render_heatmap_from_transitions(
            {
                "state_transitions": [
                    {"timestamp": "2026-03-08T06:30:00Z", "from_state": "UNKNOWN", "to_state": "FLOW"},
                    {"timestamp": "2026-03-08T08:30:00Z", "from_state": "FLOW", "to_state": "HYPO"},
                ],
                "end_time": "2026-03-08T09:00:00Z",
            }
        )
        buckets = panel._heatmap._buckets
        assert buckets[1:5] == [1800.0, 0.0, 3600.0, 1800.0]
    finally:
        panel.deleteLater()


def test_naive_stamp_is_read_as_utc(utc_minus_five: None) -> None:
    assert ht._format_date_time("2026-11-25T20:14:00") == "Wed 11/25 · 3:14 PM"
