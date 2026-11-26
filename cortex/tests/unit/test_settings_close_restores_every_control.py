"""Close must restore every Settings control, not only the ones once saved.

Follow-up to the close-discards-edits fix. Three gaps let an abandoned edit
survive Close and ride along on the next unrelated Apply:

* weekly-schedule slots were reset only when a schedule had been saved, so
  before the first Apply an edited slot stayed edited;
* the WebSocket host's ``apply_payload`` re-saved a snapshot of every
  control on each SETTINGS_SYNC -- unapplied edits included -- so Close then
  "restored" the edit;
* with the rule-based backend saved, the planner mode was never reloaded.
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
        widget.hide()
        widget.deleteLater()


def test_unsaved_schedule_edit_is_discarded(dialog: settings_mod.SettingsDialog) -> None:
    emitted: list[dict] = []
    dialog.settings_changed.connect(emitted.append)
    dialog._schedule_combos["monday"][0].setCurrentText("off")

    dialog._on_back()
    dialog._apply_settings()

    assert dialog._schedule_combos["monday"][0].currentText() == "on"
    assert emitted[-1]["weekly_schedule"]["monday"][0] == "on"


def test_sync_does_not_save_an_unapplied_edit(dialog: settings_mod.SettingsDialog) -> None:
    dialog._cooldown_spin.setValue(120)

    dialog.apply_payload({"quiet_mode": False, "quiet_duration_minutes": 0})
    dialog._on_back()

    assert dialog._cooldown_spin.value() == 60


def test_synced_keys_are_still_saved(dialog: settings_mod.SettingsDialog) -> None:
    dialog.apply_payload({"interventions_enabled": False})

    dialog._on_back()

    assert not dialog._interventions_enabled.isChecked()


def test_planner_mode_resets_with_rule_based_backend_saved(
    dialog: settings_mod.SettingsDialog,
) -> None:
    dialog._llm_backend.setCurrentIndex(3)  # Rule-based (offline)
    dialog._apply_settings()
    assert dialog._llm_privacy_mode.currentIndex() == 0

    dialog._llm_privacy_mode.setCurrentIndex(1)
    dialog._on_back()

    assert dialog._llm_backend.currentIndex() == 3
    assert dialog._llm_privacy_mode.currentIndex() == 0
