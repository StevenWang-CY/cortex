"""A successful external request must not inherit a previous failure's verdict.

The result page is built once and reused. ``apply_error`` for a confirmation
rewrites its mark to "!" and its title to "Request was not sent";
``apply_confirmation`` only rewrote the detail and plan text. After one
failed attempt (provider timeout, revoked token) every later success was
shown under a warning mark reading "Request was not sent", directly above
the validated plan that had in fact been received.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from PySide6.QtWidgets import QApplication

from cortex.apps.desktop_shell.context_privacy_dialog import ContextPrivacyDialog


@pytest.fixture()
def dialog() -> Iterator[ContextPrivacyDialog]:
    _app = QApplication.instance() or QApplication([])
    widget = ContextPrivacyDialog()
    try:
        yield widget
    finally:
        widget.deleteLater()


def test_success_after_failure_reads_as_success(dialog: ContextPrivacyDialog) -> None:
    dialog.apply_error({"operation": "confirm", "message": "The provider did not answer."})
    assert dialog._result_title.text() == "Request was not sent"

    dialog.apply_confirmation(
        {"plan": {"headline": "Close two tabs", "micro_steps": ["Close the docs tab"]}}
    )

    assert dialog._stack.currentIndex() == 2
    assert dialog._result_title.text() == "Request sent once"
    assert dialog._result_mark.text() == "✓"
    assert "Close two tabs" in dialog._result_plan.text()


def test_failure_after_success_reads_as_failure(dialog: ContextPrivacyDialog) -> None:
    dialog.apply_confirmation({"plan": {"headline": "Close two tabs"}})
    dialog.apply_error({"operation": "confirm", "message": "The provider did not answer."})

    assert dialog._result_title.text() == "Request was not sent"
    assert dialog._result_mark.text() == "!"
