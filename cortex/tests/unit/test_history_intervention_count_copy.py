"""History's "no interventions" copy must respect the report's real count.

Follow-up to 5cc8f83 / 42fdd05. Both empty states were driven by the number
of HYPER state changes, but the report carries a real
``interventions_triggered`` counter and the schema says not to conflate the
two. A session with two interventions and only a FLOW -> HYPO change showed
"No interventions in this session — focus held steady." and "No
interventions recorded in this session." at once.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from PySide6.QtWidgets import QApplication, QLabel

from cortex.apps.desktop_shell import history_tab as ht


@pytest.fixture()
def panel() -> Iterator[ht._DetailPanel]:
    _app = QApplication.instance() or QApplication([])
    widget = ht._DetailPanel()
    try:
        yield widget
    finally:
        widget.deleteLater()


def _texts(host: object) -> list[str]:
    return [label.text() for label in host.findChildren(QLabel)]  # type: ignore[attr-defined]


def _report(triggered: int) -> dict[str, object]:
    return {
        "report": {
            "session_id": "s1",
            "start_time": "2026-11-25T20:00:00Z",
            "end_time": "2026-11-25T21:00:00Z",
            "duration_seconds": 3600,
            "top_distraction_domains": [],
            "interventions_triggered": triggered,
            "state_transitions": [
                {"timestamp": "2026-11-25T20:10:00Z", "from_state": "FLOW", "to_state": "HYPO"},
            ],
        }
    }


def test_counted_interventions_are_not_denied(panel: ht._DetailPanel) -> None:
    panel.apply_payload(_report(2))

    every_text = " ".join(_texts(panel._distractions_host) + _texts(panel._interventions_host))
    assert "No interventions" not in every_text
    assert _texts(panel._interventions_host) == ["2 interventions in this session."]


def test_a_single_intervention_is_singular(panel: ht._DetailPanel) -> None:
    panel.apply_payload(_report(1))

    assert _texts(panel._interventions_host) == ["1 intervention in this session."]


def test_none_counted_keeps_the_calm_copy(panel: ht._DetailPanel) -> None:
    panel.apply_payload(_report(0))

    assert _texts(panel._distractions_host) == [
        "No interventions in this session — focus held steady."
    ]
    assert _texts(panel._interventions_host) == ["No interventions recorded in this session."]
