"""Audit F53 — QSettings sync() failure surfacing.

Pre-fix, ``SettingsDialog._persist_settings`` wrapped
``self._qs.sync()`` in a bare ``except Exception: pass``. A sandbox
container with a revoked ACL, a read-only filesystem, or a disk-full
condition all manifested as the Apply button succeeding from the user's
perspective while no setting actually persisted. F53 adds a
``settings_save_failed(str)`` Signal that the dialog emits whenever
``sync()`` raises OR ``QSettings.status()`` reports anything other than
``NoError``.

Two cases:

1. Successful sync → no signal emission.
2. Patched failure (``sync`` raises OR status reports AccessError) →
   signal emitted with a human-readable reason.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    return app


def test_successful_sync_emits_no_failure_signal(qapp: QApplication) -> None:
    from cortex.apps.desktop_shell.settings import SettingsDialog

    dialog = SettingsDialog()
    received: list[str] = []
    dialog.settings_save_failed.connect(received.append)

    dialog._persist_settings(dialog.get_settings())

    assert received == [], (
        f"settings_save_failed should not fire on the happy path; got {received}"
    )


def test_sync_exception_emits_failure_signal(qapp: QApplication) -> None:
    """If ``sync()`` raises, the reason must reach the connected slot."""
    from cortex.apps.desktop_shell.settings import SettingsDialog

    dialog = SettingsDialog()
    received: list[str] = []
    dialog.settings_save_failed.connect(received.append)

    def _raise_sync():
        raise OSError(13, "permission denied")

    with patch.object(dialog._qs, "sync", _raise_sync):
        dialog._persist_settings({"foo": "bar"})

    assert len(received) == 1, f"expected one failure signal, got {received}"
    assert "permission denied" in received[0]


def test_status_access_error_emits_failure_signal(qapp: QApplication) -> None:
    """If ``sync()`` returns clean but ``status()`` reports AccessError,
    the failure signal must still fire (the silent-failure path the
    audit was filed for)."""
    from cortex.apps.desktop_shell.settings import SettingsDialog

    dialog = SettingsDialog()
    received: list[str] = []
    dialog.settings_save_failed.connect(received.append)

    access_error = getattr(QSettings.Status, "AccessError", 1)

    with patch.object(dialog._qs, "status", lambda: access_error):
        dialog._persist_settings({"baz": "qux"})

    assert len(received) == 1, (
        f"expected one failure signal for AccessError, got {received}"
    )
    assert "access" in received[0].lower() or "AccessError" in received[0]
