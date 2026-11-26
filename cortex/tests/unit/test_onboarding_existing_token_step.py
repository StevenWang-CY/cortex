"""A Bedrock token already in the Keychain completes the Claude access step.

The wizard reads the Keychain while it builds the step and shows "Token
found in Keychain", but the step only counted as done after Get Started.
Until then the progress strip left dot 3 open and the finish note listed
"Not finished: … Claude access …" directly under the card saying the token
was found -- the usual state after reinstalling Cortex, because the Keychain
item outlives the app's own files.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from cortex.apps.desktop_shell import mac_native
from cortex.apps.desktop_shell import onboarding as onb_mod


def _wizard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, token: str | None,
) -> onb_mod.OnboardingWindow:
    _app = QApplication.instance() or QApplication([])
    from cortex.libs.utils import secrets

    monkeypatch.setattr(mac_native, "apply_vibrancy", lambda *a, **kw: False)
    monkeypatch.setattr(mac_native, "apply_unified_titlebar", lambda *a, **kw: False)
    monkeypatch.setattr(onb_mod, "_detect_continuity_camera", lambda: False)
    monkeypatch.setattr(onb_mod, "check_camera_permission", lambda: False)
    monkeypatch.setattr(onb_mod, "check_accessibility_permission", lambda: False)
    monkeypatch.setattr(onb_mod, "onboarding_state_path", lambda: tmp_path / "state.json")
    # Never read the developer's real Keychain.
    monkeypatch.setattr(secrets, "get_password_safe", lambda *a, **kw: token)
    return onb_mod.OnboardingWindow()


@pytest.fixture()
def cleanup() -> Iterator[list[onb_mod.OnboardingWindow]]:
    windows: list[onb_mod.OnboardingWindow] = []
    yield windows
    for window in windows:
        window.deleteLater()


def test_found_token_counts_as_done(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cleanup: list[onb_mod.OnboardingWindow],
) -> None:
    window = _wizard(monkeypatch, tmp_path, "t" * 120)
    cleanup.append(window)

    assert "Token found" in window._llm_status.text()
    assert "llm_backend" in window._onboarding_state.completed_steps
    assert 2 in window._progress.completed_indices()
    assert "Claude access" not in window._finish_note.text()


def test_no_token_leaves_the_step_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cleanup: list[onb_mod.OnboardingWindow],
) -> None:
    window = _wizard(monkeypatch, tmp_path, None)
    cleanup.append(window)

    assert "llm_backend" not in window._onboarding_state.completed_steps
    assert "Claude access" in window._finish_note.text()
