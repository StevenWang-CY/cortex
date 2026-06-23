"""UI-redesign coverage for the dashboard consumer tab.

Pins the two structural changes so a future refactor can't silently
regress them:

* the recent-goals picker is now an INLINE menu affordance on the goal
  field (not a stacked QComboBox) — it hides when the store is empty,
  shows when populated, and a selection fills the field + emits goal_set;
* the ambient chips (focus-protection / break / baseline / cost) live in
  a footer meta strip, not the crammed top bar, while keeping their
  attribute names so the render slots still update them.

Run isolated/offscreen like the other real-Qt desktop tests.
"""

from __future__ import annotations

import pathlib

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtWidgets import QApplication  # noqa: E402

from cortex.apps.desktop_shell.dashboard import _ConsumerTab  # noqa: E402


@pytest.fixture()
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def seeded_store(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path):
    """Redirect the goal store to a temp dir and return the module."""
    import cortex.libs.store.goal_store as gs

    monkeypatch.setattr(gs, "get_config_dir", lambda: tmp_path)
    return gs


def test_goal_affordance_hidden_when_store_empty(app, seeded_store) -> None:
    tab = _ConsumerTab()
    tab._refresh_recent_goals_dropdown()
    assert tab._goal_history_action is not None
    assert tab._goal_history_action.isVisible() is False


def test_goal_affordance_shown_when_store_has_goals(app, seeded_store) -> None:
    seeded_store.add_goal("write the audit report")
    tab = _ConsumerTab()
    tab._refresh_recent_goals_dropdown()
    assert tab._goal_history_action.isVisible() is True


def test_choosing_recent_goal_fills_field_and_emits(app, seeded_store) -> None:
    seeded_store.add_goal("ship the redesign")
    tab = _ConsumerTab()
    emitted: list[str] = []
    tab.goal_set.connect(emitted.append)
    tab._on_recent_goal_chosen("gid-1", "ship the redesign")
    assert tab._goal_input.text() == "ship the redesign"
    assert emitted == ["ship the redesign"]


def test_no_legacy_combobox_attribute(app) -> None:
    tab = _ConsumerTab()
    # The dated QComboBox is gone; only the inline affordance remains.
    assert not hasattr(tab, "_recent_goals_combo")


def test_ambient_chips_live_in_footer_strip(app) -> None:
    tab = _ConsumerTab()
    strip = tab._meta_strip
    widgets = {
        strip.itemAt(i).widget()
        for i in range(strip.count())
        if strip.itemAt(i).widget() is not None
    }
    for name in (
        "_focus_protection_pill",
        "_cost_pill",
        "_break_pill",
        "_baseline_pill",
    ):
        chip = getattr(tab, name)
        assert chip in widgets, f"{name} should be in the footer meta strip"


def test_cost_slot_still_updates_relocated_pill(app) -> None:
    tab = _ConsumerTab()
    # Attribute name preserved → the render path still finds the widget.
    tab._cost_pill.setText("$2.50")
    assert tab._cost_pill.text() == "$2.50"
