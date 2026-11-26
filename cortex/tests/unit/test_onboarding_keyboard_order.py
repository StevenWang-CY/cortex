"""The setup wizard's Tab order must follow its steps top to bottom.

``chain_tab_order`` pinned the step 3-6 actions and Get Started but not the
"Why?" disclosures of steps 4-6 (or the permission Grant buttons). Qt moves
chained widgets ahead of unchained ones, so after Get Started, Tab jumped
back up to "Why Cortex needs Calibration", then Extensions, then
Notifications -- a keyboard user reached the explanations only after the
button that finishes setup.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget

from cortex.apps.desktop_shell import mac_native
from cortex.apps.desktop_shell import onboarding as onb_mod


@pytest.fixture()
def wizard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> Iterator[onb_mod.OnboardingWindow]:
    _app = QApplication.instance() or QApplication([])
    from cortex.libs.utils import secrets

    monkeypatch.setattr(mac_native, "apply_vibrancy", lambda *a, **kw: False)
    monkeypatch.setattr(mac_native, "apply_unified_titlebar", lambda *a, **kw: False)
    monkeypatch.setattr(onb_mod, "_detect_continuity_camera", lambda: False)
    monkeypatch.setattr(onb_mod, "check_camera_permission", lambda: False)
    monkeypatch.setattr(onb_mod, "check_accessibility_permission", lambda: False)
    monkeypatch.setattr(onb_mod, "onboarding_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(secrets, "get_password_safe", lambda *a, **kw: None)
    window = onb_mod.OnboardingWindow()
    window.show()
    try:
        yield window
    finally:
        window.hide()
        window.deleteLater()


def _tab_chain(root: QWidget, start: QWidget) -> list[QWidget]:
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


def test_tab_walks_the_steps_in_order(wizard: onb_mod.OnboardingWindow) -> None:
    steps = (
        wizard._camera_step,
        wizard._accessibility_step,
        wizard._llm_step,
        wizard._calibration_step,
        wizard._extensions_step,
        wizard._notifications_step,
    )
    whys = [step._cortex_why_btn for step in steps]  # type: ignore[attr-defined]
    chain = _tab_chain(wizard, whys[0])
    expected = [
        whys[0],
        whys[1],
        whys[2],
        wizard._key_input,
        whys[3],
        wizard._begin_calibration_btn,
        whys[4],
        wizard._connect_btn_ref,
        whys[5],
        wizard._notif_btn_ref,
        wizard._finish_btn_ref,
    ]
    positions = [chain.index(widget) for widget in expected]
    assert positions == sorted(positions), [
        widget.accessibleName() or type(widget).__name__ for widget in chain
    ]
