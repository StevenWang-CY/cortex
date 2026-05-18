"""Audit F33 — goal input debounce.

Holding the Return key while the goal QLineEdit has focus fires Qt's
auto-repeated ``returnPressed`` signal at ~30 Hz. Pre-fix every press
emitted ``goal_set`` directly, which the daemon turned into N rapid-fire
planner calls (LLM cost + latency burst). The fix coalesces the burst
into a single 150 ms-delayed emission.

The test holds the Return key for 5 presses inside the 150 ms window
and asserts the ``goal_set`` signal fired exactly once.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _pyside6_is_mocked() -> bool:
    """test_desktop_shell.py installs lightweight mock PySide6 modules
    that have no ``__file__``. Re-importing real PySide6 segfaults."""
    pyside6 = sys.modules.get("PySide6")
    if pyside6 is None:
        return False
    return getattr(pyside6, "__file__", None) is None


PySide6 = pytest.importorskip("PySide6")
from PySide6.QtCore import QCoreApplication  # noqa: E402
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


def test_hold_return_emits_goal_set_exactly_once(qapp: QApplication) -> None:
    from cortex.apps.desktop_shell.dashboard import DashboardWindow

    win = DashboardWindow()
    consumer = win._consumer  # type: ignore[attr-defined]

    received: list[str] = []
    consumer.goal_set.connect(received.append)

    # Type a goal and simulate the Qt auto-repeat: returnPressed fires
    # five times in quick succession (the time it takes to call
    # ``_schedule_goal_emit`` is << 150 ms, so all five fall inside the
    # coalescer window).
    consumer._goal_input.setText("write the audit report")
    for _ in range(5):
        consumer._goal_input.returnPressed.emit()

    # Pump the Qt event loop until the single-shot timer fires (the
    # timer is 150 ms; we wait up to 1 s).
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and len(received) < 1:
        QCoreApplication.processEvents()
        time.sleep(0.01)

    assert received == ["write the audit report"], (
        f"expected exactly one goal_set emission for the burst, "
        f"got {len(received)}: {received}"
    )


def test_separate_bursts_emit_separately(qapp: QApplication) -> None:
    """Two distinct bursts (with the timer flush between them) must each
    produce one emission — the coalescer is per-burst, not a one-shot."""
    from cortex.apps.desktop_shell.dashboard import DashboardWindow

    win = DashboardWindow()
    consumer = win._consumer  # type: ignore[attr-defined]

    received: list[str] = []
    consumer.goal_set.connect(received.append)

    # First burst.
    consumer._goal_input.setText("first goal")
    for _ in range(3):
        consumer._goal_input.returnPressed.emit()
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and len(received) < 1:
        QCoreApplication.processEvents()
        time.sleep(0.01)
    assert len(received) == 1
    assert received[0] == "first goal"

    # Second burst with a different text — the coalescer must reset
    # after the first timer fired.
    consumer._goal_input.setText("second goal")
    for _ in range(3):
        consumer._goal_input.returnPressed.emit()
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and len(received) < 2:
        QCoreApplication.processEvents()
        time.sleep(0.01)
    assert received == ["first goal", "second goal"]
