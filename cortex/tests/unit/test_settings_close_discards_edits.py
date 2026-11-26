"""Closing Settings without Apply must not leave unapplied edits on screen.

The controller builds one SettingsDialog at startup and only hides it on
Close, Escape or the title-bar button. Edits made and then abandoned stayed in
the widgets, so the next time the user opened Settings it showed, say,
sensitivity 5 and a 120 s cooldown while the daemon and the saved preferences
still ran 3 and 60 s -- and a later Apply of something unrelated silently
sent the abandoned values too.
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


def test_close_button_discards_unapplied_edits(dialog: settings_mod.SettingsDialog) -> None:
    dialog._sensitivity_slider.setValue(5)
    dialog._cooldown_spin.setValue(120)

    dialog._on_back()

    assert dialog._sensitivity_slider.value() == 3
    assert dialog._sensitivity_label.text() == "3"
    assert dialog._cooldown_spin.value() == 60


def test_title_bar_close_discards_unapplied_edits(dialog: settings_mod.SettingsDialog) -> None:
    dialog.show()
    dialog._cooldown_spin.setValue(120)

    dialog.close()

    assert dialog._cooldown_spin.value() == 60


def test_applied_edits_survive_close(dialog: settings_mod.SettingsDialog) -> None:
    dialog._sensitivity_slider.setValue(4)
    dialog._apply_settings()

    dialog._on_back()

    assert dialog._sensitivity_slider.value() == 4


def test_abandoned_edit_is_not_sent_by_a_later_apply(dialog: settings_mod.SettingsDialog) -> None:
    emitted: list[dict] = []
    dialog.settings_changed.connect(emitted.append)
    dialog._cooldown_spin.setValue(120)
    dialog._on_back()

    dialog._sensitivity_slider.setValue(2)
    dialog._apply_settings()

    assert emitted[-1]["cooldown_seconds"] == 60
    assert emitted[-1]["sensitivity"] == 2


def test_close_keeps_the_last_reviewed_context_sources(
    dialog: settings_mod.SettingsDialog,
) -> None:
    # Sources picked in the review sheet are remembered for the next review;
    # they are not a Settings edit that Close should throw away.
    dialog._relay_context_preview_request({"selection": {"user_goal": True}})

    dialog._on_back()

    assert dialog._saved_context_sources == {"user_goal": True}
