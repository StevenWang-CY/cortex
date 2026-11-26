"""An elided label must keep the text it was last given.

``install_elide`` captured the label's text once, at install time, and
re-elided that string on every resize. Callers update these labels with a
plain ``setText`` (the History trends "Updated …" caption goes through the
render cache), so the next resize put the install-time text back. Pressing
Refresh on Week or Month changes the button between "Refresh" and
"Refreshing…", which resizes the caption beside it: after every refresh the
caption read "Not yet aggregated" again, and the render cache then kept it
that way until the timestamp changed.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from cortex.apps.desktop_shell.components import install_elide


@pytest.fixture()
def qapp() -> Iterator[QApplication]:
    app = QApplication.instance() or QApplication([])
    yield app  # type: ignore[misc]


def test_resize_keeps_text_set_after_install(qapp: QApplication) -> None:
    host = QWidget()
    try:
        label = QLabel("Not yet aggregated", host)
        install_elide(label)
        host.show()  # resize events reach only shown widgets
        label.resize(300, 20)

        label.setText("Updated 3h ago")
        label.resize(280, 20)

        assert label.text() == "Updated 3h ago"
    finally:
        host.hide()
        host.deleteLater()


def test_elision_applies_to_the_new_text(qapp: QApplication) -> None:
    host = QWidget()
    try:
        label = QLabel("short", host)
        filt = install_elide(label)
        assert filt is not None
        host.show()
        label.resize(400, 20)

        label.setText("A much longer caption that cannot fit in a narrow label")
        label.resize(60, 20)

        shown = label.text()
        assert shown != "short"
        assert shown.endswith("…")
        assert label.toolTip() == "A much longer caption that cannot fit in a narrow label"

        label.resize(600, 20)
        assert label.text() == "A much longer caption that cannot fit in a narrow label"
    finally:
        host.hide()
        host.deleteLater()


def test_trends_caption_survives_a_refresh(qapp: QApplication) -> None:
    from cortex.apps.desktop_shell import history_tab as ht

    panel = ht._TrendsPanel(window="week")
    try:
        panel.resize(420, 500)
        panel.show()
        qapp.processEvents()
        stamp = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
        payload = {"window": "week", "last_aggregated": stamp, "daily": []}
        panel.apply_payload(payload)
        qapp.processEvents()
        assert panel._updated_label.text().startswith("Updated")

        panel._on_refresh_clicked()
        qapp.processEvents()
        panel.apply_payload(dict(payload))
        qapp.processEvents()

        assert panel._updated_label.text().startswith("Updated"), panel._updated_label.text()
    finally:
        panel.hide()
        panel.deleteLater()
