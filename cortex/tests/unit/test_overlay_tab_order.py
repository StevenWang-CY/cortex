"""The suggestion card's Tab order must follow the card top to bottom.

"Review suggestion" focuses the card's × button. The card chained only the
Why / Snooze / Quiet / × controls once, at construction; every per-proposal
control (micro-step checkboxes, action buttons, the rating buttons) was
appended wherever Qt put it. From × the next Tab jumped to 👍 at the bottom
of the card, then back up to the steps, and the window itself was a dead Tab
stop between the actions and "Why this?".
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget

from cortex.apps.desktop_shell import mac_native
from cortex.apps.desktop_shell.overlay import OverlayWindow


@pytest.fixture()
def card(monkeypatch: pytest.MonkeyPatch) -> Iterator[OverlayWindow]:
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(mac_native, "apply_vibrancy", lambda *a, **kw: False)
    monkeypatch.setattr(mac_native, "apply_unified_titlebar", lambda *a, **kw: False)
    monkeypatch.setattr(mac_native, "screen_share_active", lambda: False)
    window = OverlayWindow()
    window.show_intervention(
        {
            "intervention_id": "i1",
            "headline": "Close the tabs you finished with",
            "level": "guided_mode",
            "micro_steps": ["Close the docs tab", "Close the PR tab"],
            "causal_explanation": "Many tabs switched in the last few minutes.",
            "suggested_actions": [
                {"action_type": "close_tab", "action_id": "a1", "label": "Close 3 tabs"}
            ],
        }
    )
    window._reveal_feedback_row()
    app.processEvents()
    try:
        yield window
    finally:
        window.hide()
        window.deleteLater()


def _tab_chain(root: QWidget, start: QWidget) -> list[QWidget]:
    chain = [start]
    widget = start.nextInFocusChain()
    while widget is not start and len(chain) < 100:
        if widget.focusPolicy() & Qt.FocusPolicy.TabFocus and widget.isVisibleTo(root):
            chain.append(widget)
        widget = widget.nextInFocusChain()
    return chain


def test_tab_walks_the_card_top_to_bottom(card: OverlayWindow) -> None:
    chain = _tab_chain(card, card._dismiss_btn)
    expected = [
        card._dismiss_btn,
        *card._step_widgets,
        *card._action_buttons,
        card._causal_toggle,
        card._snooze_btn,
        card._quiet_session_btn,
        card._thumbs_up_btn,
        card._thumbs_down_btn,
    ]
    positions = [chain.index(widget) for widget in expected]
    assert positions == sorted(positions), [
        widget.accessibleName() or type(widget).__name__ for widget in chain
    ]
    assert card not in chain, "the window itself must not be a Tab stop"


def test_a_new_proposal_keeps_the_order(card: OverlayWindow) -> None:
    card.show_intervention(
        {
            "intervention_id": "i2",
            "headline": "Take a short break",
            "level": "guided_mode",
            "micro_steps": ["Stand up", "Stretch", "Sit back down"],
        }
    )
    QApplication.processEvents()  # new children are shown by a queued call
    chain = _tab_chain(card, card._dismiss_btn)
    positions = [chain.index(widget) for widget in [card._dismiss_btn, *card._step_widgets, card._snooze_btn]]
    assert positions == sorted(positions)
