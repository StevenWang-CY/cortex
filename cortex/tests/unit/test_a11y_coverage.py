"""Audit Wave 2 — accessible names on every interactive surface.

F55 added accessible names + tab order to the dashboard and overlay.
This commit extends the coverage to the connections panel, settings
dialog, and onboarding wizard. The test instantiates each panel under
``QT_QPA_PLATFORM=offscreen`` and asserts that key interactive widgets
have a non-empty ``accessibleName``. Without these, VoiceOver announces
the controls as "button" / "checkbox" with no semantic context.

Run with: ``QT_QPA_PLATFORM=offscreen pytest cortex/tests/unit/test_a11y_coverage.py``
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Drop any stale PySide6 mocks installed by other test modules so the
# real PySide6 (with accessibleName) is loaded.
for _name in list(sys.modules):
    if _name == "PySide6" or _name.startswith("PySide6."):
        mod = sys.modules[_name]
        if not hasattr(mod, "__file__") or "site-packages" not in str(
            getattr(mod, "__file__", "") or ""
        ):
            del sys.modules[_name]

import pytest  # noqa: E402

try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    pytest.skip("PySide6 not available", allow_module_level=True)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_settings_dialog_accessibility(qapp, tmp_path, monkeypatch):
    """Settings dialog must expose accessible names on every control."""
    # Steer QSettings to a temp file so the test doesn't touch the user's
    # real preferences.
    monkeypatch.setenv("HOME", str(tmp_path))

    from cortex.apps.desktop_shell.settings import SettingsDialog

    dialog = SettingsDialog()
    try:
        assert dialog._webcam_enabled.accessibleName() == "Enable webcam"
        assert (
            dialog._input_telemetry_enabled.accessibleName()
            == "Enable keyboard and mouse tracking"
        )
        assert dialog._interventions_enabled.accessibleName() == "Enable interventions"
        assert dialog._sensitivity_slider.accessibleName() == "Intervention sensitivity"
        assert (
            dialog._cooldown_spin.accessibleName()
            == "Intervention cooldown (seconds)"
        )
        assert dialog._quiet_mode.accessibleName() == "Quiet mode"
        assert (
            dialog._quiet_duration.accessibleName()
            == "Quiet duration (minutes)"
        )
        assert dialog._llm_backend.accessibleName() == "LLM backend provider"
        # VoiceOver hint also wired on the sensitivity slider.
        assert (
            "Lower values"
            in dialog._sensitivity_slider.accessibleDescription()
        )
    finally:
        dialog.deleteLater()


def test_connections_panel_accessibility(qapp):
    """ConnectionsPanel must expose accessible names on the buttons."""
    from cortex.apps.desktop_shell.connections import ConnectionsPanel

    panel = ConnectionsPanel()
    try:
        # Every chained widget has a non-empty accessible name.
        assert panel._tab_order_chain, "tab-order chain is empty"
        for widget in panel._tab_order_chain:
            name = widget.accessibleName()
            assert name, f"{widget!r} has no accessibleName"
        # Back button is first.
        assert panel._tab_order_chain[0].accessibleName() == "Back to dashboard"
    finally:
        panel.deleteLater()


def test_onboarding_window_accessibility(qapp, tmp_path, monkeypatch):
    """OnboardingWindow must expose accessible names on its inputs."""
    # F49 onboarding-state file lives under config_dir — keep test
    # isolated from the user's real state.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        "cortex.apps.desktop_shell.onboarding.OnboardingState.load",
        classmethod(lambda cls, path=None: cls()),
    )

    from cortex.apps.desktop_shell.onboarding import OnboardingWindow

    win = OnboardingWindow()
    try:
        # Stop the polling timer so the test does not race against it.
        try:
            win._permission_timer.stop()
        except Exception:
            pass
        assert win._key_input.accessibleName() == "Bedrock bearer token"
        assert win._region_combo.accessibleName() == "Bedrock AWS region"
        assert win._save_key_btn.accessibleName() == (
            "Save Bedrock bearer token"
        )
        # Camera + accessibility grant buttons live inside the step
        # frames; we walk the frame's children to find a QPushButton
        # whose accessible name starts with the step title.
        from PySide6.QtWidgets import QPushButton

        for step_frame, expected_prefix in (
            (win._camera_step, "Camera access —"),
            (win._accessibility_step, "Accessibility —"),
        ):
            buttons = step_frame.findChildren(QPushButton)
            assert buttons, f"{expected_prefix} has no buttons"
            assert any(
                btn.accessibleName().startswith(expected_prefix)
                for btn in buttons
            ), (
                f"{expected_prefix} grant button missing accessibleName "
                f"(found: {[b.accessibleName() for b in buttons]!r})"
            )
    finally:
        win.deleteLater()
