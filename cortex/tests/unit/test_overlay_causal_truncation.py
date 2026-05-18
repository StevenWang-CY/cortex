"""Audit F51 — causal explanation truncation indicator.

Long causal-explanation strings used to be dumped into the overlay
verbatim — overflowing the HUD card, pushing the breathing pacer and
dismiss button below the fold, and giving the user no affordance to
scan a one-line summary first. F51 truncates to a one-line preview
with a trailing ellipsis when the text exceeds
``OverlayWindow._CAUSAL_TRUNCATE_THRESHOLD`` and surfaces a "Show
more" QToolButton (checkable) that toggles between preview and full.

Cases:
1. Short text → no ellipsis, no toggle.
2. Long text → preview has trailing "…", toggle button visible and
   says "Show more".
3. Clicking the toggle expands the label to the full text and the
   button label flips to "Show less".
4. Clicking again collapses back to the preview.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    return app


def test_short_causal_no_ellipsis(qapp: QApplication) -> None:
    """We invoke the truncation helper directly to avoid triggering the
    overlay's showEvent (which on macOS reaches into Cocoa's
    NSVisualEffectView APIs and can segfault under the offscreen Qt
    platform plugin)."""
    from cortex.apps.desktop_shell.overlay import OverlayWindow

    win = OverlayWindow()
    win._show_causal_explanation("Brief reason for this intervention")
    assert "…" not in win._causal_label.text()
    # Short text → toggle stays hidden, even before any show() call.
    assert not win._causal_toggle.isVisibleTo(win.parentWidget())  # smoke
    # The label text starts with "Why this?" prefix.
    assert win._causal_label.text().startswith("Why this?")


def test_long_causal_has_ellipsis_and_toggle(qapp: QApplication) -> None:
    from cortex.apps.desktop_shell.overlay import OverlayWindow

    win = OverlayWindow()
    long_text = (
        "Your heart rate has been elevated for the past 20 minutes while "
        "you've been switching between Slack, Gmail, and three different "
        "Notion pages — a pattern Cortex associates with reactive "
        "task-switching rather than focused work, and your HRV has "
        "dropped twelve points below your seven-day baseline."
    )
    assert len(long_text) > OverlayWindow._CAUSAL_TRUNCATE_THRESHOLD
    win._show_causal_explanation(long_text)
    # The visible label text ends with the ellipsis sentinel.
    assert win._causal_label.text().endswith("…")
    # The toggle button is checkable and labelled "Show more" pre-click.
    assert win._causal_toggle.text() == "Show more"
    assert win._causal_toggle.isCheckable()
    # The visible preview is shorter than the full "Why this? <text>".
    assert len(win._causal_label.text()) < len(f"Why this? {long_text}")


def test_clicking_show_more_expands_text(qapp: QApplication) -> None:
    from cortex.apps.desktop_shell.overlay import OverlayWindow

    win = OverlayWindow()
    long_text = "A " * 200  # well over the 180-char threshold
    win._show_causal_explanation(long_text)
    preview = win._causal_label.text()
    assert preview.endswith("…")

    # Toggle on.
    win._causal_toggle.setChecked(True)
    assert win._causal_toggle.text() == "Show less"
    expanded = win._causal_label.text()
    assert expanded != preview
    assert long_text.strip() in expanded

    # Toggle off — should collapse back to the preview.
    win._causal_toggle.setChecked(False)
    assert win._causal_toggle.text() == "Show more"
    assert win._causal_label.text() == preview


def test_hide_causal_resets_everything(qapp: QApplication) -> None:
    """_hide_causal_explanation must clear the cached strings + reset
    the toggle so a subsequent show is not contaminated by stale state."""
    from cortex.apps.desktop_shell.overlay import OverlayWindow

    win = OverlayWindow()
    win._show_causal_explanation("X" * 300)
    win._causal_toggle.setChecked(True)
    win._hide_causal_explanation()
    assert win._causal_label.text() == ""
    assert not win._causal_toggle.isChecked()
    assert win._causal_full_text == ""
    assert win._causal_preview_text == ""

