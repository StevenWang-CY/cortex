"""History's report must not say "No interventions" above its interventions.

When a session recorded no distracting sites, the "Top distractions" section
fell back to "No interventions in this session — focus held steady." even
when the "Interventions" section right below listed support moments from
that same session -- two sections of one report contradicting each other.
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


def _report(transitions: list[dict[str, str]]) -> dict[str, object]:
    return {
        "report": {
            "session_id": "s1",
            "start_time": "2026-11-25T20:00:00Z",
            "end_time": "2026-11-25T21:00:00Z",
            "duration_seconds": 3600,
            "top_distraction_domains": [],
            "state_transitions": transitions,
        }
    }


def test_no_sites_but_interventions_does_not_deny_them(panel: ht._DetailPanel) -> None:
    panel.apply_payload(
        _report(
            [
                {"timestamp": "2026-11-25T20:10:00Z", "from_state": "FLOW", "to_state": "HYPER"},
            ]
        )
    )

    distraction_copy = " ".join(_texts(panel._distractions_host))
    assert "No interventions" not in distraction_copy
    assert len(_texts(panel._interventions_host)) == 1


def test_quiet_session_keeps_the_calm_copy(panel: ht._DetailPanel) -> None:
    panel.apply_payload(_report([]))

    assert _texts(panel._distractions_host) == [
        "No interventions in this session — focus held steady."
    ]
