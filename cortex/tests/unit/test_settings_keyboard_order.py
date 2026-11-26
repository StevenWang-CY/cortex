"""Settings must be operable in visual order from the keyboard.

``chain_tab_order`` pins a sequence of controls but left out the camera and
accessibility permission links, "Recalibrate profile" and "Test
connection". Qt moves every chained widget ahead of those, so Tab ran the
whole window down to Apply and only then jumped back up to the Sensing
links, Recalibrate and Test connection. The custom-domains box also kept
Tab for itself, inserting a tab character -- never valid in a domain --
instead of moving on.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from cortex.apps.desktop_shell import mac_native
from cortex.apps.desktop_shell import settings as settings_mod


@pytest.fixture()
def dialog(monkeypatch: pytest.MonkeyPatch) -> Iterator[settings_mod.SettingsDialog]:
    _app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(mac_native, "apply_vibrancy", lambda *a, **kw: False)
    monkeypatch.setattr(mac_native, "apply_unified_titlebar", lambda *a, **kw: False)
    widget = settings_mod.SettingsDialog()
    widget.show()
    try:
        yield widget
    finally:
        widget.hide()
        widget.deleteLater()


def _tab_chain(root: QWidget, start: QWidget) -> list[QWidget]:
    """Widgets Tab can land on, in focus-chain order, starting at *start*."""
    chain = [start]
    widget = start.nextInFocusChain()
    while widget is not start and len(chain) < 400:
        if (
            widget.focusPolicy() & Qt.FocusPolicy.TabFocus
            and widget.isVisibleTo(root)
            and widget.isEnabled()
        ):
            chain.append(widget)
        widget = widget.nextInFocusChain()
    return chain


def test_tab_reaches_every_control_in_visual_order(
    dialog: settings_mod.SettingsDialog,
) -> None:
    chain = _tab_chain(dialog, dialog._webcam_enabled)
    camera_link = dialog._camera_perm_row["label"]
    accessibility_link = dialog._accessibility_perm_row["label"]
    apply_btn = dialog._apply_btn
    assert apply_btn is not None

    expected = [
        dialog._webcam_enabled,
        camera_link,
        dialog._input_telemetry_enabled,
        accessibility_link,
        dialog._recalibrate_btn,
        dialog._interventions_enabled,
        dialog._llm_backend,
        dialog._test_provider_btn,
        dialog._llm_privacy_mode,
        dialog._rotate_token_btn,
        apply_btn,
    ]
    positions = [chain.index(widget) for widget in expected]
    assert positions == sorted(positions), [
        widget.accessibleName() or type(widget).__name__ for widget in chain
    ]


def test_tab_leaves_the_custom_domains_box(dialog: settings_mod.SettingsDialog) -> None:
    box = dialog._distraction_custom_domains
    box.setPlainText("example.com")

    QTest.keyClick(box, Qt.Key.Key_Tab)

    assert "\t" not in box.toPlainText()
    assert box.tabChangesFocus()
