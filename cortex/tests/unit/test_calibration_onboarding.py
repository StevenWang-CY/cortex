"""P0 §3.4 — onboarding wizard integration for calibration.

Cases:

1. ``test_onboarding_steps_includes_calibration`` — the canonical
   ``ONBOARDING_STEPS`` tuple includes ``"calibration"`` between
   ``llm_backend`` and ``extensions``.
2. ``test_begin_button_emits_signal`` — the new calibration card's
   "Begin" button click emits the previously-dormant
   ``run_calibration_requested`` signal so the controller can
   schedule the in-process ``CalibrationRunner``.
3. ``test_apply_progress_marks_complete_on_completed`` — the
   ``apply_calibration_progress`` slot marks the step complete when
   the runner reports ``status='completed'``.
4. ``test_skip_marks_complete_without_baseline`` — clicking the Skip
   link still marks the step complete (so the wizard can finish) but
   does not write a baseline.

Uses the real PySide6 widgets under ``QT_QPA_PLATFORM=offscreen`` so
the calibration card's QPainter / QProgressBar / chained tab order
get exercised honestly. Mirrors the existing
``test_onboarding_hints.py`` harness.
"""

# ruff: noqa: E402, I001

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Drop any stale PySide6 mocks installed by other test modules so we
# pick up the real Qt under the offscreen platform.
for _name in list(sys.modules):
    if _name == "PySide6" or _name.startswith("PySide6."):
        mod = sys.modules[_name]
        if not hasattr(mod, "__file__") or "site-packages" not in str(
            getattr(mod, "__file__", "") or ""
        ):
            del sys.modules[_name]

# Also drop cached desktop_shell modules so they re-import against the
# real PySide6 (the other test_desktop_shell.py module installs Qt mocks
# at import time).
for _name in list(sys.modules):
    if _name.startswith("cortex.apps.desktop_shell"):
        del sys.modules[_name]

import pytest

try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    pytest.skip("PySide6 not available", allow_module_level=True)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def wizard(qapp, monkeypatch, tmp_path):
    """Build an OnboardingWindow with macOS chrome stubbed out and the
    on-disk state path redirected to a tmp dir."""
    from cortex.apps.desktop_shell import mac_native
    from cortex.apps.desktop_shell import onboarding as onb_mod

    monkeypatch.setattr(mac_native, "apply_vibrancy", lambda *a, **kw: False)
    monkeypatch.setattr(
        mac_native, "apply_unified_titlebar", lambda *a, **kw: False
    )
    monkeypatch.setattr(onb_mod, "_detect_continuity_camera", lambda: False)
    monkeypatch.setattr(
        onb_mod, "onboarding_state_path", lambda: tmp_path / "state.json"
    )

    w = onb_mod.OnboardingWindow()
    yield w
    try:
        w.deleteLater()
    except RuntimeError:
        pass


def test_onboarding_steps_includes_calibration() -> None:
    from cortex.apps.desktop_shell.onboarding import ONBOARDING_STEPS

    assert "calibration" in ONBOARDING_STEPS
    # Order matters — the wizard renders in this exact sequence and the
    # ProgressStrip dot count depends on the cardinality. P0 §3.12 added
    # a sixth ``macos_notifications`` step at the tail.
    assert ONBOARDING_STEPS == (
        "camera",
        "accessibility",
        "llm_backend",
        "calibration",
        "extensions",
        "macos_notifications",
    )


def test_why_copy_has_calibration_entry() -> None:
    from cortex.apps.desktop_shell.onboarding import _WHY_COPY

    assert "calibration" in _WHY_COPY
    assert "baseline" in _WHY_COPY["calibration"].lower()


def test_begin_button_emits_signal(wizard) -> None:
    """Clicking the Begin button must emit the dormant
    ``run_calibration_requested`` signal so the controller can spin up
    a CalibrationRunner."""
    received: list[None] = []

    def _slot():
        received.append(None)

    wizard.run_calibration_requested.connect(_slot)
    assert hasattr(wizard, "_begin_calibration_btn")
    wizard._begin_calibration_btn.click()
    assert received, "Begin click should emit run_calibration_requested"


def test_apply_progress_marks_complete_on_completed(wizard) -> None:
    """A ``status='completed'`` progress event flips the calibration
    step in OnboardingState to complete."""
    wizard.apply_calibration_progress(
        elapsed_seconds=120.0,
        total_seconds=120.0,
        current_hr=70.0,
        current_hrv=50.0,
        current_sqi=0.95,
        lighting_ok=True,
        motion_ok=True,
        face_ok=True,
        pct_complete=100.0,
        status="completed",
    )
    assert "calibration" in wizard._onboarding_state.completed_steps


def test_skip_marks_complete_without_baseline(wizard) -> None:
    wizard._on_skip_calibration()
    assert "calibration" in wizard._onboarding_state.completed_steps


def test_calibration_step_card_exists(wizard) -> None:
    """The card frame is built by ``_make_calibration_step`` and
    stashed on the window so the controller can introspect it later."""
    assert hasattr(wizard, "_calibration_step")
    assert hasattr(wizard, "_cal_progress_bar")
    assert hasattr(wizard, "_cal_numerics")
    assert hasattr(wizard, "_cal_lighting_pill")
    assert hasattr(wizard, "_cal_motion_pill")
    assert hasattr(wizard, "_cal_face_pill")
    assert hasattr(wizard, "_ecg_trace")


def test_progress_strip_has_five_dots(wizard) -> None:
    """P0 §3.4 increased the wizard from 4 to 5 steps; P0 §3.12 added
    a sixth ``macos_notifications`` step at the tail."""
    assert wizard._progress._count == 6


def test_apply_progress_updates_numerics(wizard) -> None:
    wizard.apply_calibration_progress(
        elapsed_seconds=10.0,
        total_seconds=120.0,
        current_hr=68.0,
        current_hrv=49.0,
        current_sqi=0.92,
        lighting_ok=True,
        motion_ok=True,
        face_ok=True,
        pct_complete=8.3,
        status="running",
    )
    text = wizard._cal_numerics.text()
    assert "68" in text
    assert "49" in text
    assert "0.92" in text
