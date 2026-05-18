"""Audit F48 — breathing pacer cadence is configurable.

The 4-7-8 pattern (inhale 4 s, hold 7 s, exhale 8 s) is the default
relaxation cadence Dr. Andrew Weil recommends, but users with COPD,
anxiety disorders, or simply different lung capacity sometimes prefer
shorter or longer counts. Pre-fix the values were hardcoded at module
level (_INHALE_SECONDS = 4 etc.) and could not be tuned without patching
the source. F48 moves the spec to
``InterventionConfig.breathing_pattern`` and ``BreathingPacer`` reads
from there at construction time, falling back to 4-7-8 if no config is
available.
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
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    return app


def test_intervention_config_has_breathing_pattern_default() -> None:
    from cortex.libs.config.settings import InterventionConfig

    cfg = InterventionConfig()
    assert tuple(cfg.breathing_pattern) == (4, 7, 8)


def test_pacer_honours_explicit_pattern_argument(qapp: QApplication) -> None:
    from cortex.apps.desktop_shell.overlay import BreathingPacer

    pacer = BreathingPacer(pattern=(2, 3, 5))
    assert pacer._inhale == 2
    assert pacer._hold == 3
    assert pacer._exhale == 5
    assert pacer._cycle == 10


def test_pacer_reads_breathing_pattern_from_config(
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Patch the global config to return a 3-5-7 pattern and confirm a
    pacer constructed without an explicit pattern picks it up."""
    from cortex.libs.config.settings import InterventionConfig

    class _ProxyConfig:
        intervention = InterventionConfig(breathing_pattern=(3, 5, 7))

    monkeypatch.setattr(
        "cortex.libs.config.settings.get_config",
        lambda: _ProxyConfig(),
    )

    from cortex.apps.desktop_shell.overlay import BreathingPacer

    pacer = BreathingPacer()
    assert pacer._inhale == 3
    assert pacer._hold == 5
    assert pacer._exhale == 7
    assert pacer._cycle == 15
    # monkeypatch automatically restores ``get_config`` after the test.


def test_pacer_phase_math_uses_configured_cadence(qapp: QApplication) -> None:
    """The _get_phase() output must reflect the configured pattern, not
    the legacy 4-7-8 constants."""
    from cortex.apps.desktop_shell.overlay import BreathingPacer

    pacer = BreathingPacer(pattern=(2, 3, 5))
    pacer.start()

    # At elapsed = 0 ms we're at the start of the inhale phase.
    pacer._elapsed_ms = 0
    phase, remaining, scale = pacer._get_phase()
    assert phase == "Inhale"
    assert remaining == pytest.approx(2.0)
    # At elapsed = 2 100 ms (just past inhale boundary), we're in hold.
    pacer._elapsed_ms = 2100
    phase, remaining, _ = pacer._get_phase()
    assert phase == "Hold"
    # At elapsed = 5 100 ms (past inhale + hold), we're in exhale.
    pacer._elapsed_ms = 5100
    phase, remaining, _ = pacer._get_phase()
    assert phase == "Exhale"
