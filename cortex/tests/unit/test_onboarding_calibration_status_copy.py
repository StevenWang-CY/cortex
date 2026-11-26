"""The onboarding calibration card must say how the run actually ended.

The runner reports ``review_required`` -> ``applying`` -> ``completed`` for a
saved profile. The card's status line is rewritten for ``review_required``
("Ready to review · nothing has been saved yet") and ``applying`` ("Saving and
applying measured profile…"), but ``completed`` only re-showed it, so a
successful calibration ended on "Saving and applying…" forever. A failed or
discarded run's "Application not confirmed" line also stayed visible through
the next attempt.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtWidgets import QApplication

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
    # Never read the developer's real Keychain.
    monkeypatch.setattr(secrets, "get_password_safe", lambda *a, **kw: None)
    window = onb_mod.OnboardingWindow()
    try:
        yield window
    finally:
        window.deleteLater()


def _progress(status: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "elapsed_seconds": 0.0,
        "total_seconds": 120.0,
        "current_hr": None,
        "current_hrv": None,
        "current_sqi": None,
        "lighting_ok": True,
        "motion_ok": True,
        "face_ok": True,
        "pct_complete": 100.0,
        "status": status,
    }
    payload.update(extra)
    return payload


def test_completed_run_reads_as_saved(wizard: onb_mod.OnboardingWindow) -> None:
    wizard._on_begin_calibration()
    for status in ("running", "review_required", "applying", "completed"):
        wizard.apply_calibration_progress(**_progress(status))

    label = wizard._cal_success_label
    assert label.isVisibleTo(wizard)
    assert label.text() == "✓ Measured profile saved and applied"
    assert "calibration" in wizard._onboarding_state.completed_steps


def test_new_attempt_clears_the_previous_failure(wizard: onb_mod.OnboardingWindow) -> None:
    wizard._on_begin_calibration()
    wizard.apply_calibration_progress(**_progress("failed"))
    assert wizard._cal_success_label.text().startswith("Application not confirmed")

    wizard._on_begin_calibration()

    assert not wizard._cal_success_label.isVisibleTo(wizard)
    wizard.apply_calibration_progress(**_progress("running", pct_complete=10.0))
    assert not wizard._cal_success_label.isVisibleTo(wizard)
