"""F34 — Dashboard Stop button + tray Quit action disable during shutdown.

Run with: ``QT_QPA_PLATFORM=offscreen pytest cortex/tests/unit/test_dashboard_stop.py``

The button (and the tray's Quit action) disables on the first click, swaps
its text to "Stopping…", and re-enables only when the controller emits
``daemon_stopped`` or the safety timer fires. The safety timeout is
configurable per-test via ``set_stop_safety_timeout_ms`` so we can wait
1 s instead of 10 s.
"""

from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Drop any stale PySide6 mocks installed by other test modules.
for _name in list(sys.modules):
    if _name == "PySide6" or _name.startswith("PySide6."):
        mod = sys.modules[_name]
        if not hasattr(mod, "__file__") or "site-packages" not in str(
            getattr(mod, "__file__", "") or ""
        ):
            del sys.modules[_name]

import pytest  # noqa: E402

try:
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    pytest.skip("PySide6 not available", allow_module_level=True)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def dashboard(qapp, monkeypatch):
    from cortex.apps.desktop_shell import dashboard as dashboard_mod
    from cortex.apps.desktop_shell import mac_native

    # Offscreen Qt has no real NSWindow; stub out the macOS-only calls.
    monkeypatch.setattr(mac_native, "apply_vibrancy", lambda *a, **kw: False)
    monkeypatch.setattr(
        mac_native, "apply_unified_titlebar", lambda *a, **kw: False
    )

    w = dashboard_mod.DashboardWindow()
    yield w
    try:
        w.deleteLater()
    except RuntimeError:
        pass


def _process_for(ms: int) -> None:
    """Pump the Qt event loop for ~``ms`` milliseconds so timers fire."""
    deadline = time.monotonic() + ms / 1000.0
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.01)


def test_first_click_disables_button(dashboard):
    consumer = dashboard._consumer
    emissions: list[None] = []
    dashboard.stop_requested.connect(lambda: emissions.append(None))

    assert consumer._stop_btn.isEnabled()
    assert consumer._stop_btn.text() == "Stop Cortex"

    consumer._handle_stop_clicked()

    assert len(emissions) == 1
    assert not consumer._stop_btn.isEnabled()
    assert consumer._stop_btn.text() == "Stopping…"
    assert consumer._stopping is True
    assert consumer._stop_safety_timer.isActive()


def test_double_click_emits_once(dashboard):
    consumer = dashboard._consumer
    emissions: list[None] = []
    dashboard.stop_requested.connect(lambda: emissions.append(None))

    consumer._handle_stop_clicked()
    consumer._handle_stop_clicked()
    consumer._handle_stop_clicked()

    assert len(emissions) == 1, (
        f"double-click should coalesce; got {len(emissions)} emissions"
    )
    assert not consumer._stop_btn.isEnabled()


def test_stuck_shutdown_reenables_after_safety_timeout(dashboard):
    """If the daemon never reports ``daemon_stopped`` the safety timer must
    re-enable the button so the user can retry."""
    consumer = dashboard._consumer
    # Shorten the budget for the test — production default is 10 s.
    dashboard.set_stop_safety_timeout_ms(200)

    consumer._handle_stop_clicked()
    assert not consumer._stop_btn.isEnabled()

    _process_for(800)

    assert consumer._stop_btn.isEnabled(), (
        "safety timer must re-enable Stop button when daemon never reports"
    )
    assert consumer._stop_btn.text() == "Stop Cortex"
    assert consumer._stopping is False
    assert not consumer._stop_safety_timer.isActive()


def test_daemon_stopped_reenables(dashboard):
    consumer = dashboard._consumer

    consumer._handle_stop_clicked()
    assert not consumer._stop_btn.isEnabled()

    dashboard.notify_daemon_stopped()

    assert consumer._stop_btn.isEnabled()
    assert consumer._stop_btn.text() == "Stop Cortex"
    assert consumer._stopping is False


def test_tray_quit_disables_during_shutdown(qapp, monkeypatch):
    from cortex.apps.desktop_shell import mac_native
    from cortex.apps.desktop_shell import tray as tray_mod

    monkeypatch.setattr(mac_native, "apply_vibrancy", lambda *a, **kw: False)
    monkeypatch.setattr(
        mac_native, "apply_unified_titlebar", lambda *a, **kw: False
    )

    tray = tray_mod.CortexTrayIcon(qapp)
    tray.set_stop_safety_timeout_ms(200)

    emissions: list[None] = []
    tray.quit_requested.connect(lambda: emissions.append(None))

    # First trigger emits and disables.
    tray._handle_quit_triggered()
    assert len(emissions) == 1
    assert not tray._quit_action.isEnabled()
    assert tray._quit_action.text() == "Stopping…"

    # Double trigger coalesces.
    tray._handle_quit_triggered()
    assert len(emissions) == 1

    # Safety timer re-enables.
    _process_for(800)
    assert tray._quit_action.isEnabled()
    assert tray._quit_action.text() == "Quit Cortex"
    assert tray._stopping is False
