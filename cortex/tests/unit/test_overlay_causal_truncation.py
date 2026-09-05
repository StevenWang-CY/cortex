"""Overlay "Why this?" — a single affordance for the causal explanation.

The suggestion card no longer shows a truncated preview competing with
a second "Show more" control. The causal explanation lives, in full,
inside the Why panel behind one checkable "Why this?" toggle:

1. Text present → toggle visible, panel collapsed, full text stored
   (no ellipsis, no truncation threshold).
2. Checking the toggle expands the panel and relabels it "Hide why".
3. Unchecking collapses the panel and restores "Why this?".
4. ``_hide_causal_explanation`` clears the text, hides the toggle, and
   resets its checked state so a later suggestion starts clean.
5. Opening the panel with no cached signals asks the daemon for them
   (``why_requested``) exactly once per open.
"""

from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _pyside6_is_mocked() -> bool:
    """test_desktop_shell.py installs lightweight mock PySide6 modules
    that have no ``__file__``. Re-importing real PySide6 segfaults."""
    pyside6 = sys.modules.get("PySide6")
    if pyside6 is None:
        return False
    return getattr(pyside6, "__file__", None) is None


pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(autouse=True)
def _skip_if_pyside6_mocked():
    """Skip when test_desktop_shell.py's mocks override real PySide6."""
    if _pyside6_is_mocked():
        pytest.skip(
            "PySide6 mocked by earlier test in session — run in isolation",
        )


@pytest.fixture(scope="module")
def qapp():
    if _pyside6_is_mocked():
        pytest.skip("PySide6 mocked", allow_module_level=False)
    qt_widgets = sys.modules.get("PySide6.QtWidgets")
    real_qapp = getattr(qt_widgets, "QApplication", None)
    if real_qapp is None:
        pytest.skip("PySide6.QtWidgets unavailable", allow_module_level=False)
    app = getattr(real_qapp, "instance", lambda: None)() or real_qapp([])
    return app


_LONG = (
    "Your heart rate has been elevated for the past 20 minutes while "
    "you've been switching between Slack, Gmail, and three different "
    "Notion pages — a pattern Cortex associates with reactive "
    "task-switching rather than focused work, and your HRV has "
    "dropped twelve points below your seven-day baseline."
)


def test_causal_text_is_never_truncated(qapp: QApplication) -> None:
    from cortex.apps.desktop_shell.overlay import OverlayWindow

    win = OverlayWindow()
    assert not hasattr(OverlayWindow, "_CAUSAL_TRUNCATE_THRESHOLD")
    win._show_causal_explanation(_LONG)
    assert win._causal_full_text == _LONG
    assert win._causal_label.text() == _LONG
    assert "…" not in win._causal_label.text()


def test_single_why_toggle_controls_the_panel(qapp: QApplication) -> None:
    from cortex.apps.desktop_shell.overlay import OverlayWindow

    win = OverlayWindow()
    win._show_causal_explanation(_LONG)
    assert win._causal_toggle.isVisibleTo(win)
    assert win._causal_toggle.isCheckable()
    assert win._causal_toggle.text() == "Why this?"
    assert not win._why_panel.isVisibleTo(win)
    # There is exactly one affordance — no legacy "Show more" control.
    assert not hasattr(win, "_why_toggle") or win._why_toggle is win._causal_toggle

    win._causal_toggle.setChecked(True)
    assert win._causal_toggle.text() == "Hide why"
    assert win._why_panel.isVisibleTo(win)
    assert win._causal_label.isVisibleTo(win)

    win._causal_toggle.setChecked(False)
    assert win._causal_toggle.text() == "Why this?"
    assert not win._why_panel.isVisibleTo(win)


def test_hide_causal_resets_everything(qapp: QApplication) -> None:
    from cortex.apps.desktop_shell.overlay import OverlayWindow

    win = OverlayWindow()
    win._show_causal_explanation("X" * 300)
    win._causal_toggle.setChecked(True)
    win._hide_causal_explanation()
    assert win._causal_label.text() == ""
    assert win._causal_full_text == ""
    assert not win._causal_toggle.isChecked()
    assert not win._causal_toggle.isVisibleTo(win)
    assert not win._why_panel.isVisibleTo(win)


def test_opening_why_requests_signals_once(qapp: QApplication) -> None:
    from cortex.apps.desktop_shell.overlay import OverlayWindow

    win = OverlayWindow()
    requested: list[str] = []
    win.why_requested.connect(requested.append)
    win._intervention_id = "iv_1"
    win._show_causal_explanation(_LONG)

    win._causal_toggle.setChecked(True)
    assert requested == ["iv_1"]

    # Once signals are cached, re-opening does not ask again.
    win.apply_causal_signals([{"label": "Tab switches", "value": "14 in 10 min"}])
    win._causal_toggle.setChecked(False)
    win._causal_toggle.setChecked(True)
    assert requested == ["iv_1"]
