"""The dashboard's break offer belongs to the live session that earned it.

Found in review: ``apply_break_recommendation`` never looked at the session
phase and nothing cleared the pill when a session stopped, so a queued
recommendation appeared after Stop and a click in the next session started a
strain break that session had not earned.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from PySide6.QtWidgets import QApplication

from cortex.apps.desktop_shell import dashboard as dashboard_module
from cortex.apps.desktop_shell.dashboard import DashboardWindow

STRAIN = {
    "basis": "physiological_strain",
    "reason": "Your camera readings have stayed away from your usual level for a while.",
    "duration_seconds": 180,
    "breathing_pattern": "box",
}
ELAPSED = {
    "basis": "elapsed_focus",
    "reason": "You've reached your preferred focus interval.",
    "focus_elapsed_seconds": 3000.0,
    "duration_seconds": 300,
}


@pytest.fixture()
def tab(monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    from cortex.apps.desktop_shell import mac_native

    _app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(mac_native, "apply_vibrancy", lambda *a, **kw: False)
    monkeypatch.setattr(mac_native, "apply_unified_titlebar", lambda *a, **kw: False)
    window = DashboardWindow()
    try:
        yield window._consumer
    finally:
        window.hide()
        window.deleteLater()


def test_a_live_session_shows_each_eligible_offer(tab) -> None:
    tab._enter_phase(dashboard_module._PHASE_LIVE)

    tab.apply_break_recommendation(dict(STRAIN))
    assert not tab._break_pill.isHidden()
    assert tab._break_pill.text() == "Take a breather? · 3 min"
    assert tab._break_recommendation_payload["basis"] == "physiological_strain"

    tab.apply_break_recommendation(dict(ELAPSED))
    assert not tab._break_pill.isHidden()
    assert tab._break_pill.text() == "Take a break? · 50 min in"


@pytest.mark.parametrize("basis", ["legacy_stress_integral", None, "something_else"])
def test_an_ineligible_basis_shows_nothing_and_clears_an_earlier_offer(tab, basis) -> None:
    tab._enter_phase(dashboard_module._PHASE_LIVE)
    tab.apply_break_recommendation(dict(STRAIN))
    assert not tab._break_pill.isHidden()

    tab.apply_break_recommendation({**STRAIN, "basis": basis})
    assert tab._break_pill.isHidden()
    assert tab._break_recommendation_payload == {}


@pytest.mark.parametrize(
    "phase",
    [
        dashboard_module._PHASE_DISCONNECTED,
        dashboard_module._PHASE_STARTING,
        dashboard_module._PHASE_STOPPING,
        dashboard_module._PHASE_ENDED,
    ],
)
def test_an_offer_that_arrives_outside_a_live_session_is_dropped(tab, phase: str) -> None:
    tab._enter_phase(phase)
    tab.apply_break_recommendation(dict(STRAIN))
    assert tab._break_pill.isHidden()
    assert tab._break_recommendation_payload == {}


@pytest.mark.parametrize(
    "phase",
    [
        dashboard_module._PHASE_STOPPING,
        dashboard_module._PHASE_ENDED,
        dashboard_module._PHASE_DISCONNECTED,
    ],
)
def test_ending_the_session_takes_the_offer_away(tab, phase: str) -> None:
    tab._enter_phase(dashboard_module._PHASE_LIVE)
    tab.apply_break_recommendation(dict(STRAIN))
    assert not tab._break_pill.isHidden()

    tab._enter_phase(phase)
    assert tab._break_pill.isHidden()
    assert tab._break_recommendation_payload == {}

    # The next session starts without it; a click has nothing to send.
    requested: list[dict] = []
    tab.break_requested.connect(requested.append) if hasattr(tab, "break_requested") else None
    tab._enter_phase(dashboard_module._PHASE_LIVE)
    assert tab._break_pill.isHidden()


@pytest.mark.parametrize("ratio", [0.42, 0.85, 1.0, 1.6])
def test_the_body_load_line_states_the_load_and_promises_nothing(tab, ratio: float) -> None:
    """Whether a break is then suggested depends on the user's switch, quiet
    mode and the interruption gate; the label used to say "a short break
    would help" / "nearing a break" regardless."""

    tab._render_strain_load(ratio)
    text = tab._strain_label.text()
    assert text == f"Body load {int(round(ratio * 100))}% of your break level"
    for promise in ("would help", "nearing", "break soon", "time for"):
        assert promise not in text.lower()
    assert not tab._strain_label.isHidden()
    tab._render_strain_load(None)
    assert tab._strain_label.isHidden()
