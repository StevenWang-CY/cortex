"""F34 — Dashboard Stop button + tray Quit action disable during shutdown.

Run with: ``QT_QPA_PLATFORM=offscreen pytest cortex/tests/unit/test_dashboard_stop.py``

The button (and the tray's Quit action) disables on the first click, swaps
its text to "Stopping…", and re-enables only when the controller emits
``daemon_stopped`` or the safety timer fires. The safety timeout is
configurable per-test via ``set_stop_safety_timeout_ms`` so we can wait
1 s instead of 10 s.

P0 §3.3 / Phase 4.B two-phase Stop flow (updated contract):

* First click → ``_arm_stop`` disables the button + shows "Stopping…" +
  arms the recap watchdog AND emits ``daemon_stop_requested`` immediately
  (so the controller can schedule ``daemon.stop()`` and the SESSION_RECAP
  pipeline can actually run). The legacy ``stop_requested`` is an alias
  for ``daemon_stop_requested`` and is also emitted on click.
* SESSION_RECAP arrives → recap sheet shows → user dismisses →
  ``_finalize_stop`` emits ``gui_quit_requested`` exactly once.
* If no recap arrives, the 6 s watchdog fires → ``_finalize_stop`` emits
  ``gui_quit_requested``.
* ``_finalize_stop`` is idempotent — double clicks / multiple paths
  cannot emit ``gui_quit_requested`` twice.
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


def test_first_click_fires_daemon_stop_but_defers_quit(dashboard):
    """Phase 4.B (#1): first click fires ``daemon_stop_requested`` (and
    the legacy ``stop_requested`` alias) IMMEDIATELY so the controller
    can schedule ``daemon.stop()`` — without this, the SESSION_RECAP
    pipeline never runs in DMG mode.

    ``gui_quit_requested`` stays deferred to ``_finalize_stop`` which
    fires when the recap sheet dismisses OR the 6 s watchdog expires.
    """
    consumer = dashboard._consumer
    daemon_emissions: list[None] = []
    quit_emissions: list[None] = []
    legacy_emissions: list[None] = []
    dashboard.daemon_stop_requested.connect(
        lambda: daemon_emissions.append(None)
    )
    dashboard.gui_quit_requested.connect(
        lambda: quit_emissions.append(None)
    )
    dashboard.stop_requested.connect(
        lambda: legacy_emissions.append(None)
    )

    assert consumer._stop_btn.isEnabled()
    assert consumer._stop_btn.text() == "Stop Cortex"

    consumer._handle_stop_clicked()

    # Daemon-stop fires immediately so the daemon can broadcast SESSION_RECAP.
    assert len(daemon_emissions) == 1, (
        "daemon_stop_requested must fire on click so daemon.stop() runs"
    )
    # Legacy alias also fires (backwards compat).
    assert len(legacy_emissions) == 1, (
        "legacy stop_requested alias must fire on click for backwards compat"
    )
    # gui_quit is still deferred — the user hasn't seen the recap yet.
    assert len(quit_emissions) == 0, (
        "gui_quit_requested must not fire until recap dismissed or watchdog fires"
    )
    assert not consumer._stop_btn.isEnabled()
    assert consumer._stop_btn.text() == "Stopping…"
    assert consumer._stopping is True
    assert consumer._stop_safety_timer.isActive()
    # Recap watchdog must be armed and waiting.
    assert consumer._recap_watchdog is not None
    assert consumer._recap_watchdog.isActive()
    assert consumer._recap_finalised is False


def test_recap_watchdog_expiry_emits_gui_quit(dashboard):
    """When the 6 s recap watchdog fires (no recap arrived), the
    deferred ``gui_quit_requested`` finally emits so Qt can exit."""
    consumer = dashboard._consumer
    quit_emissions: list[None] = []
    dashboard.gui_quit_requested.connect(
        lambda: quit_emissions.append(None)
    )

    consumer._handle_stop_clicked()
    assert len(quit_emissions) == 0

    # Simulate the watchdog timeout firing (don't actually wait 6 s).
    consumer._on_recap_watchdog_expired()
    assert len(quit_emissions) == 1
    assert consumer._recap_finalised is True


def test_apply_session_recap_then_dismiss_emits_gui_quit(
    dashboard, monkeypatch
):
    """Recap arrives → sheet shows → user dismisses → finalise emits
    ``gui_quit_requested``."""
    consumer = dashboard._consumer
    quit_emissions: list[None] = []
    dashboard.gui_quit_requested.connect(
        lambda: quit_emissions.append(None)
    )

    consumer._handle_stop_clicked()
    assert len(quit_emissions) == 0

    # ``apply_session_recap`` lives on the parent DashboardWindow. We
    # patch the RecapSheet class so the constructor doesn't try to
    # render real Qt widgets.
    class _FakeSheet:
        def __init__(self, parent=None):
            self.dismissed = _FakeSignal()
            self.view_full_report = _FakeSignal()

        def show_report(self, payload):
            self._payload = payload

    class _FakeSignal:
        def __init__(self) -> None:
            self._cbs: list = []

        def connect(self, cb):
            self._cbs.append(cb)

        def emit(self, *args):
            for cb in self._cbs:
                cb(*args)

    import cortex.apps.desktop_shell.recap_sheet as recap_mod

    monkeypatch.setattr(recap_mod, "RecapSheet", _FakeSheet)
    payload = {"session_id": "s1", "duration_seconds": 600.0}
    dashboard.apply_session_recap(payload)

    # Sheet was constructed; still no quit emit until dismiss.
    assert dashboard._recap_sheet is not None
    assert len(quit_emissions) == 0

    # User dismisses → finalise emits.
    dashboard._recap_sheet.dismissed.emit()
    assert len(quit_emissions) == 1


def test_double_click_emits_gui_quit_once_after_watchdog(dashboard):
    """Pressing Stop twice yields exactly one ``gui_quit_requested``.
    Idempotency contract from P0 §3.3.

    Note: the daemon_stop signal can fire multiple times via the legacy
    alias if the user mashes the button — but the consumer's
    ``_stopping`` flag coalesces so only the first click does work.
    What matters is that ``gui_quit_requested`` fires exactly once."""
    consumer = dashboard._consumer
    quit_emissions: list[None] = []
    dashboard.gui_quit_requested.connect(
        lambda: quit_emissions.append(None)
    )

    consumer._handle_stop_clicked()
    consumer._handle_stop_clicked()
    consumer._handle_stop_clicked()
    # Still no quit emit at this point — the watchdog hasn't fired.
    assert len(quit_emissions) == 0
    assert not consumer._stop_btn.isEnabled()

    # Now drive the watchdog manually and confirm exactly one quit emit.
    consumer._on_recap_watchdog_expired()
    consumer._on_recap_watchdog_expired()  # idempotent
    assert len(quit_emissions) == 1


def test_stuck_shutdown_reenables_after_safety_timeout(dashboard):
    """If the daemon never reports ``daemon_stopped`` the safety timer must
    re-enable the button so the user can retry — and ``gui_quit_requested``
    must have been emitted by ``_finalize_stop`` along the way so Qt
    exits cleanly on the slow path."""
    consumer = dashboard._consumer
    quit_emissions: list[None] = []
    dashboard.gui_quit_requested.connect(
        lambda: quit_emissions.append(None)
    )
    # Shorten the budget for the test — production default is 10 s.
    dashboard.set_stop_safety_timeout_ms(200)

    consumer._handle_stop_clicked()
    assert not consumer._stop_btn.isEnabled()
    assert len(quit_emissions) == 0

    _process_for(800)

    assert consumer._stop_btn.isEnabled(), (
        "safety timer must re-enable Stop button when daemon never reports"
    )
    assert consumer._stop_btn.text() == "Stop Cortex"
    assert consumer._stopping is False
    assert not consumer._stop_safety_timer.isActive()
    # The safety path force-finalises so the GUI exits.
    assert len(quit_emissions) == 1


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
