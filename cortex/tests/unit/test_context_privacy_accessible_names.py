"""The context review sheet's text areas need accessible names.

The optional planner note and the "Included and redacted fields" list are
plain-text areas with a visual caption above them but no accessible name,
so VoiceOver announced each as an unlabelled "text area" -- on the sheet
whose whole purpose is telling the user exactly what will leave the Mac.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from PySide6.QtWidgets import QApplication, QPlainTextEdit

from cortex.apps.desktop_shell.context_privacy_dialog import ContextPrivacyDialog


@pytest.fixture()
def dialog() -> Iterator[ContextPrivacyDialog]:
    _app = QApplication.instance() or QApplication([])
    widget = ContextPrivacyDialog()
    try:
        yield widget
    finally:
        widget.deleteLater()


def test_every_text_area_is_named(dialog: ContextPrivacyDialog) -> None:
    names = {edit.accessibleName() for edit in dialog.findChildren(QPlainTextEdit)}
    assert "" not in names
    assert dialog._extra_note.accessibleName() == "Optional note to the planner"
    assert dialog._disclosure_preview.accessibleName() == "Included and redacted fields"
