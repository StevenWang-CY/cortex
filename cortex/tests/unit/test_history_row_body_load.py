"""The History row shows the session's peak body load, and only when one exists.

It used to print ``peak 0`` for every session: the raw strain integral with no
unit, including for sessions in which the camera produced no physiology.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from PySide6.QtWidgets import QApplication

from cortex.apps.desktop_shell import history_tab as ht
from cortex.services.state_engine.strain_load import DEFAULT_THRESHOLD_SECONDS


@pytest.fixture()
def app() -> Iterator[QApplication]:
    yield QApplication.instance() or QApplication([])


def _summary(peak: float) -> dict[str, object]:
    return {
        "session_id": "s1",
        "start_time": "2026-11-25T20:00:00Z",
        "end_time": "2026-11-25T21:00:00Z",
        "duration_seconds": 3600.0,
        "flow_percentage": 40.0,
        "peak_stress_integral": peak,
        "top_distraction_domain": None,
        "intervention_count": 0,
    }


def test_a_session_without_physiology_shows_no_load(app: QApplication) -> None:
    row = ht._SessionRow(_summary(0.0))
    try:
        assert row._peak_label.isHidden()
        assert row._peak_label.text() == ""
    finally:
        row.deleteLater()


@pytest.mark.parametrize(
    ("peak", "expected"),
    [
        (0.5 * DEFAULT_THRESHOLD_SECONDS, "peak load 50%"),
        (DEFAULT_THRESHOLD_SECONDS, "peak load 100%"),
        (1.25 * DEFAULT_THRESHOLD_SECONDS, "peak load 125%"),
    ],
)
def test_peak_load_is_shown_against_the_break_level(
    app: QApplication, peak: float, expected: str
) -> None:
    row = ht._SessionRow(_summary(peak))
    try:
        assert not row._peak_label.isHidden()
        assert row._peak_label.text() == expected
        assert "break level" in row._peak_label.toolTip()
    finally:
        row.deleteLater()
