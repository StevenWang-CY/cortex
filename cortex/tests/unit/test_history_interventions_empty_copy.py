"""History must not show the internal state name in its empty state.

Everywhere else the shell maps states through ``STATE_LABELS`` and never
prints ``HYPER``/``FLOW`` (the tray contract pins that). The report's
Interventions section, when empty, read "No HYPER spikes recorded." --
engine jargon under a heading that says "Interventions".
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


def test_empty_interventions_copy_is_plain_language(panel: ht._DetailPanel) -> None:
    panel._populate_interventions([])

    texts = [label.text() for label in panel._interventions_host.findChildren(QLabel)]
    assert texts == ["No interventions recorded in this session."]
    assert not any("HYPER" in text for text in texts)
