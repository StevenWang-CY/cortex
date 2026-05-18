"""Audit F31 — dashboard re-render storm guard.

The 2 Hz state broadcast loop pushes the same payload through
``DashboardWindow.update_state`` repeatedly when the user's state is
unchanged. Pre-fix, every call invoked ``setStyleSheet`` and ``setText``
on every cached label, triggering Qt's full restyle / paint chain. The
fix is a per-widget cache that short-circuits identical writes.

The test counts setStyleSheet / setText / setValue calls under 20
consecutive identical updates: pre-fix you would see ~20 calls per
widget; post-fix at most 1 (the first one populates the cache; any
prior priming hit reduces it to 0).

The test relies on an offscreen Qt context — set
``QT_QPA_PLATFORM=offscreen`` in the test runner env. If the platform
plugin cannot initialise (headless CI without offscreen), the test
self-skips.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PySide6 = pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    return app


def _patch_count_calls(widget, attr_name: str, counter: dict) -> None:
    """Replace ``widget.<attr_name>`` with a wrapper that increments
    ``counter[(id(widget), attr_name)]`` on each call but still forwards
    to Qt."""
    original = getattr(widget, attr_name)
    key = (id(widget), attr_name)
    counter[key] = 0

    def wrapped(*args, **kwargs):
        counter[key] += 1
        return original(*args, **kwargs)

    setattr(widget, attr_name, wrapped)


def test_consumer_tab_setstylesheet_called_once_under_identical_updates(
    qapp: QApplication,
) -> None:
    from cortex.apps.desktop_shell.dashboard import DashboardWindow

    win = DashboardWindow()
    consumer = win._consumer  # type: ignore[attr-defined]

    payload = {
        "state": "FLOW",
        "biometrics": {
            "heart_rate": 72.0,
            "hrv_rmssd": 45.0,
            "blink_rate": 12.0,
        },
    }
    # Prime the cache with one update so the cache slot exists.
    consumer.update_state(payload)

    counter: dict = {}
    _patch_count_calls(consumer._state_dot, "setStyleSheet", counter)
    _patch_count_calls(consumer._state_label, "setStyleSheet", counter)
    _patch_count_calls(consumer._state_label, "setText", counter)
    _patch_count_calls(consumer._bpm_label, "setText", counter)
    _patch_count_calls(consumer._hrv_label, "setText", counter)
    _patch_count_calls(consumer._blk_label, "setText", counter)

    for _ in range(20):
        consumer.update_state(payload)

    # Every widget should see at most 1 additional write (and ideally 0)
    # because the values are byte-identical to the priming call. Pre-fix
    # this was 20 per widget (>= 120 total).
    for key, count in counter.items():
        assert count <= 1, (
            f"widget {key} was rewritten {count} times under 20 "
            "identical updates — render guard regressed"
        )


def test_consumer_tab_rewrites_when_state_changes(
    qapp: QApplication,
) -> None:
    """Sanity check: the guard must still propagate genuinely new values."""
    from cortex.apps.desktop_shell.dashboard import DashboardWindow

    win = DashboardWindow()
    consumer = win._consumer  # type: ignore[attr-defined]

    consumer.update_state(
        {"state": "FLOW", "biometrics": {"heart_rate": 70.0}}
    )
    assert consumer._bpm_label.text() == "70"

    consumer.update_state(
        {"state": "FLOW", "biometrics": {"heart_rate": 95.0}}
    )
    assert consumer._bpm_label.text() == "95"


def test_advanced_tab_progress_bars_skip_identical_values(
    qapp: QApplication,
) -> None:
    from cortex.apps.desktop_shell.dashboard import DashboardWindow

    win = DashboardWindow()
    advanced = win._advanced  # type: ignore[attr-defined]

    payload = {
        "state": "FLOW",
        "scores": {"flow": 0.8, "hyper": 0.1, "hypo": 0.05, "recovery": 0.05},
        "signal_quality": {"physio": 0.9, "kinematics": 0.7, "telemetry": 0.5},
        "confidence": 0.82,
        "dwell_seconds": 12.4,
        "biometrics": {},
    }
    advanced.update_state(payload)

    counter: dict = {}
    for name in ("flow", "hyper", "hypo", "recovery"):
        _patch_count_calls(advanced._score_bars[name], "setValue", counter)
        _patch_count_calls(advanced._score_labels[name], "setText", counter)
    _patch_count_calls(advanced._confidence_lbl, "setText", counter)
    _patch_count_calls(advanced._dwell_lbl, "setText", counter)

    for _ in range(20):
        advanced.update_state(payload)

    for key, count in counter.items():
        assert count <= 1, (
            f"advanced widget {key} was rewritten {count} times under "
            "20 identical updates"
        )
