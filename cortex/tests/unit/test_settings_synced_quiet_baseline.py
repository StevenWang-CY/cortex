"""A quiet state synced from the daemon is not a change the user made here.

Apply withholds ``quiet_mode`` unless the checkbox differs from the value the
dialog last knew (``_quiet_mode_baseline``). ``apply_payload`` -- the
WebSocket host's ``SETTINGS_SYNC`` sink -- moved the checkbox to the daemon's
state but left the baseline behind. Pausing from the tray broadcasts
``{quiet_mode: true, quiet_duration_minutes: 0}``; the next Apply of any
unrelated setting then restated ``quiet_mode: true`` with the dialog's
5-minute minimum duration, and the daemon turned the indefinite pause into a
five-minute snooze that silently resumed sensing and suggestions.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from PySide6.QtWidgets import QApplication

from cortex.apps.desktop_shell import mac_native
from cortex.apps.desktop_shell import settings as settings_mod


@pytest.fixture()
def dialog(monkeypatch: pytest.MonkeyPatch) -> Iterator[settings_mod.SettingsDialog]:
    _app = QApplication.instance() or QApplication([])
    # Start every test from an empty store. Only the conftest sandbox may be
    # cleared: the real class here would mean the developer's own prefs.
    if isinstance(settings_mod.QSettings, type):
        pytest.skip("QSettings sandbox inactive; refusing to touch the real store")
    store = settings_mod.QSettings("Cortex", "Desktop")
    store.clear()
    store.sync()
    monkeypatch.setattr(mac_native, "apply_vibrancy", lambda *a, **kw: False)
    monkeypatch.setattr(mac_native, "apply_unified_titlebar", lambda *a, **kw: False)
    widget = settings_mod.SettingsDialog()
    try:
        yield widget
    finally:
        widget.deleteLater()


def test_synced_pause_is_not_restated_by_an_unrelated_apply(
    dialog: settings_mod.SettingsDialog,
) -> None:
    emitted: list[dict] = []
    dialog.settings_changed.connect(emitted.append)

    dialog.apply_payload(
        {"quiet_mode": True, "quiet_mode_kind": "pause", "quiet_duration_minutes": 0}
    )
    assert dialog._quiet_mode.isChecked(), "precondition: the checkbox mirrors the sync"

    dialog._sensitivity_slider.setValue(4)
    dialog._apply_settings()

    assert emitted[-1]["sensitivity"] == 4
    assert "quiet_mode" not in emitted[-1]
    assert "quiet_duration_minutes" not in emitted[-1]


def test_synced_resume_is_not_restated_either(
    dialog: settings_mod.SettingsDialog,
) -> None:
    emitted: list[dict] = []
    dialog.settings_changed.connect(emitted.append)
    dialog._quiet_mode.setChecked(True)
    dialog._apply_settings()
    assert emitted[-1]["quiet_mode"] is True

    # Quiet ended elsewhere (decay, tray Resume, extension).
    dialog.apply_payload({"quiet_mode": False, "quiet_duration_minutes": 0})
    dialog._apply_settings()
    assert "quiet_mode" not in emitted[-1]


def test_toggling_after_a_sync_is_still_sent(
    dialog: settings_mod.SettingsDialog,
) -> None:
    emitted: list[dict] = []
    dialog.settings_changed.connect(emitted.append)
    dialog.apply_payload({"quiet_mode": True, "quiet_duration_minutes": 0})

    dialog._quiet_mode.setChecked(False)
    dialog._apply_settings()

    assert emitted[-1]["quiet_mode"] is False
