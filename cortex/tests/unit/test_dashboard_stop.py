"""Dashboard session controls — End session vs Quit Cortex.

Run with: ``QT_QPA_PLATFORM=offscreen pytest cortex/tests/unit/test_dashboard_stop.py``

Contract (session semantics):

* While live, the footer control reads "End session". Clicking it arms
  the two-phase stop: the button disables, reads "Ending…", the recap
  watchdog and the safety timer arm, and ``daemon_stop_requested`` (plus
  the legacy ``stop_requested`` alias) fire immediately so the daemon can
  broadcast SESSION_RECAP.
* Ending a session never quits Cortex by itself. When the recap is
  consumed (dismissed / watchdog / daemon ack) the dashboard settles into
  the *ended* phase: the footer offers "Start session" (when the host
  can restart the daemon) and an explicit "Quit Cortex" button appears.
* ``gui_quit_requested`` fires only on an explicit quit route: the
  dashboard's Quit button, the recap sheet's "Quit Cortex", or a
  user-initiated quit (tray / Cmd+Q) which arms the stop with
  ``quit_after=True``.
* The recap sheet's "View full report" keeps Cortex open even on the
  quit route — the user can change their mind on the sheet.
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
    # A live session: the daemon reported connected.
    w._consumer.set_connected(True)
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


class _FakeSignal:
    def __init__(self) -> None:
        self._cbs: list = []

    def connect(self, cb):
        self._cbs.append(cb)

    def emit(self, *args):
        for cb in self._cbs:
            cb(*args)


class _FakeSheet:
    """Stand-in for RecapSheet so the test does not render Qt widgets."""

    instances: list[_FakeSheet] = []

    def __init__(self, parent=None):
        self.dismissed = _FakeSignal()
        self.view_full_report = _FakeSignal()
        self.quit_requested = _FakeSignal()
        self.quit_pending = None
        _FakeSheet.instances.append(self)

    def show_report(self, payload, *, quit_pending=False):
        self._payload = payload
        self.quit_pending = quit_pending


def _install_fake_sheet(monkeypatch):
    import cortex.apps.desktop_shell.recap_sheet as recap_mod

    _FakeSheet.instances.clear()
    monkeypatch.setattr(recap_mod, "RecapSheet", _FakeSheet)


def test_first_click_fires_daemon_stop_but_defers_quit(dashboard):
    """First click fires ``daemon_stop_requested`` (and the legacy alias)
    IMMEDIATELY so the controller can schedule ``daemon.stop()``.
    ``gui_quit_requested`` is not part of ending a session at all."""
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
    assert consumer._stop_btn.text() == "End session"
    assert not consumer._quit_btn.isVisibleTo(consumer)

    consumer._handle_stop_clicked()

    assert len(daemon_emissions) == 1, (
        "daemon_stop_requested must fire on click so daemon.stop() runs"
    )
    assert len(legacy_emissions) == 1, (
        "legacy stop_requested alias must fire on click for backwards compat"
    )
    assert len(quit_emissions) == 0
    assert not consumer._stop_btn.isEnabled()
    assert consumer._stop_btn.text() == "Ending…"
    assert consumer._stopping is True
    assert consumer._stop_safety_timer.isActive()
    assert consumer._recap_watchdog is not None
    assert consumer._recap_watchdog.isActive()
    assert consumer._recap_finalised is False


def test_recap_watchdog_expiry_ends_session_without_quitting(dashboard):
    """No recap arrived → the watchdog completes the End-session route:
    Cortex stays open and settles into the ended phase once the daemon
    acknowledges."""
    consumer = dashboard._consumer
    quit_emissions: list[None] = []
    dashboard.gui_quit_requested.connect(
        lambda: quit_emissions.append(None)
    )
    dashboard.set_session_restart_available(True)

    consumer._handle_stop_clicked()
    consumer._on_recap_watchdog_expired()
    assert consumer._recap_finalised is True
    assert quit_emissions == []

    dashboard.notify_daemon_stopped()
    assert quit_emissions == []
    assert consumer._stopping is False
    assert consumer._stop_btn.isEnabled()
    assert consumer._stop_btn.text() == "Start session"
    assert consumer._quit_btn.isVisibleTo(consumer)


def test_quit_route_emits_gui_quit_once_after_watchdog(dashboard):
    """A user-initiated quit (tray / Cmd+Q) arms the stop with
    ``quit_after=True``; the watchdog then completes the quit exactly once."""
    consumer = dashboard._consumer
    quit_emissions: list[None] = []
    dashboard.gui_quit_requested.connect(
        lambda: quit_emissions.append(None)
    )

    consumer._arm_stop(quit_after=True)
    consumer._arm_stop(quit_after=True)
    consumer._arm_stop(quit_after=True)
    assert quit_emissions == []
    assert not consumer._stop_btn.isEnabled()

    consumer._on_recap_watchdog_expired()
    consumer._on_recap_watchdog_expired()  # idempotent
    assert len(quit_emissions) == 1


def test_recap_dismiss_keeps_cortex_open_on_end_session(dashboard, monkeypatch):
    """Recap arrives → sheet shows (Close, not Quit) → user closes it →
    no quit; the dashboard reaches the ended phase on daemon ack."""
    consumer = dashboard._consumer
    quit_emissions: list[None] = []
    dashboard.gui_quit_requested.connect(
        lambda: quit_emissions.append(None)
    )
    _install_fake_sheet(monkeypatch)

    consumer._handle_stop_clicked()
    dashboard.apply_session_recap({"session_id": "s1", "duration_seconds": 600.0})

    sheet = dashboard._recap_sheet
    assert sheet is not None
    assert sheet.quit_pending is False
    assert quit_emissions == []

    sheet.dismissed.emit()
    assert quit_emissions == []
    assert consumer._recap_finalised is True

    dashboard.notify_daemon_stopped()
    assert quit_emissions == []
    assert consumer._quit_btn.isVisibleTo(consumer)


def test_recap_sheet_quit_button_quits(dashboard, monkeypatch):
    consumer = dashboard._consumer
    quit_emissions: list[None] = []
    dashboard.gui_quit_requested.connect(
        lambda: quit_emissions.append(None)
    )
    _install_fake_sheet(monkeypatch)

    consumer._handle_stop_clicked()
    dashboard.apply_session_recap({"session_id": "s1", "duration_seconds": 600.0})
    dashboard._recap_sheet.quit_requested.emit()
    assert len(quit_emissions) == 1
    # A later dismiss cannot emit a second quit.
    dashboard._recap_sheet.dismissed.emit()
    assert len(quit_emissions) == 1


def test_quit_route_recap_sheet_offers_quit_and_view_cancels_it(
    dashboard, monkeypatch
):
    """On the quit route the sheet's primary is "Quit Cortex"; choosing
    "View full report" instead keeps Cortex open and shows the History
    tab without asking the (stopped) daemon for anything."""
    consumer = dashboard._consumer
    quit_emissions: list[None] = []
    dashboard.gui_quit_requested.connect(
        lambda: quit_emissions.append(None)
    )
    _install_fake_sheet(monkeypatch)

    consumer._arm_stop(quit_after=True)
    dashboard.apply_session_recap({"session_id": "s1", "duration_seconds": 600.0})
    sheet = dashboard._recap_sheet
    assert sheet.quit_pending is True

    sheet.view_full_report.emit("s1")
    assert quit_emissions == []
    assert consumer._recap_finalised is True
    assert consumer._quit_after_stop is False
    assert dashboard._stack.currentIndex() == 1  # History tab

    dashboard.notify_daemon_stopped()
    assert quit_emissions == []
    assert consumer._stop_btn.text() in ("Session ended", "Start session")


def test_stuck_shutdown_reenables_after_safety_timeout(dashboard):
    """If the daemon never reports stopped the safety timer finishes the
    route and settles the footer so the user is not wedged — and
    ending a session still does not quit."""
    consumer = dashboard._consumer
    quit_emissions: list[None] = []
    dashboard.gui_quit_requested.connect(
        lambda: quit_emissions.append(None)
    )
    dashboard.set_session_restart_available(True)
    dashboard.set_stop_safety_timeout_ms(200)

    consumer._handle_stop_clicked()
    assert not consumer._stop_btn.isEnabled()

    _process_for(800)

    assert consumer._stop_btn.isEnabled()
    assert consumer._stop_btn.text() == "Start session"
    assert consumer._stopping is False
    assert not consumer._stop_safety_timer.isActive()
    assert quit_emissions == []


def test_stuck_shutdown_on_quit_route_still_quits(dashboard):
    consumer = dashboard._consumer
    quit_emissions: list[None] = []
    dashboard.gui_quit_requested.connect(
        lambda: quit_emissions.append(None)
    )
    dashboard.set_stop_safety_timeout_ms(200)

    consumer._arm_stop(quit_after=True)
    _process_for(800)
    assert len(quit_emissions) == 1
    assert consumer._stopping is False


def test_daemon_stopped_settles_into_ended_phase(dashboard):
    consumer = dashboard._consumer
    dashboard.set_session_restart_available(True)

    consumer._handle_stop_clicked()
    assert not consumer._stop_btn.isEnabled()

    dashboard.notify_daemon_stopped()

    assert consumer._stop_btn.isEnabled()
    assert consumer._stop_btn.text() == "Start session"
    assert consumer._stopping is False
    assert consumer._quit_btn.isVisibleTo(consumer)
    assert consumer._quit_btn.text() == "Quit Cortex"


def test_session_ended_without_restart_offers_only_quit(dashboard):
    consumer = dashboard._consumer
    dashboard.set_session_restart_available(False)
    consumer._handle_stop_clicked()
    dashboard.notify_daemon_stopped()
    assert consumer._stop_btn.text() == "Session ended"
    assert not consumer._stop_btn.isEnabled()
    assert consumer._quit_btn.isVisibleTo(consumer)


def test_start_session_requests_restart_then_returns_to_live(dashboard):
    consumer = dashboard._consumer
    starts: list[None] = []
    dashboard.session_start_requested.connect(lambda: starts.append(None))
    dashboard.set_session_restart_available(True)

    consumer._handle_stop_clicked()
    dashboard.notify_daemon_stopped()
    assert consumer._stop_btn.text() == "Start session"

    consumer._on_primary_clicked()
    assert len(starts) == 1
    assert consumer._stop_btn.text() == "Starting…"
    assert not consumer._stop_btn.isEnabled()

    consumer.set_connected(True)
    assert consumer._stop_btn.text() == "End session"
    assert consumer._stop_btn.isEnabled()
    assert not consumer._quit_btn.isVisibleTo(consumer)


def test_explicit_quit_button_emits_gui_quit(dashboard):
    consumer = dashboard._consumer
    quit_emissions: list[None] = []
    dashboard.gui_quit_requested.connect(
        lambda: quit_emissions.append(None)
    )
    consumer._handle_stop_clicked()
    dashboard.notify_daemon_stopped()
    consumer._on_quit_clicked()
    assert len(quit_emissions) == 1


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


def test_tray_quit_names_its_consequence_while_connected(qapp):
    from cortex.apps.desktop_shell import tray as tray_mod

    tray = tray_mod.CortexTrayIcon(qapp)
    assert tray._quit_action.text() == "Quit Cortex"
    tray.set_connected(True)
    assert tray._quit_action.text() == "End Session and Quit Cortex"
    tray.set_connected(False)
    assert tray._quit_action.text() == "Quit Cortex"
