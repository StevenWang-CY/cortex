"""Desktop-shell design contracts, exercised offscreen.

Run with:
    ``QT_QPA_PLATFORM=offscreen pytest cortex/tests/unit/test_desktop_shell_contracts.py``

Pins the truthful-state and window-posture contracts of the macOS shell:

* Building the dashboard creates no stray top-level windows.
* The suggestion card shows without activating and never calls
  ``activateWindow``; it is a fixed-width notification.
* Escape closes the Settings and Connections windows (and emits
  ``back_requested`` so hosts can restore the dashboard).
* The floating toast never changes the geometry of the content beneath.
* "Get Started" records only the onboarding steps that are really done.
* The Connections window renders outcomes inline instead of modal boxes.
"""

from __future__ import annotations

import inspect
import os
import sys

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
    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    pytest.skip("PySide6 not available", allow_module_level=True)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _stub_native_chrome(monkeypatch):
    from cortex.apps.desktop_shell import mac_native

    monkeypatch.setattr(mac_native, "apply_vibrancy", lambda *a, **kw: False)
    monkeypatch.setattr(mac_native, "apply_unified_titlebar", lambda *a, **kw: False)


def test_dashboard_construction_creates_no_stray_top_level_windows(qapp):
    from cortex.apps.desktop_shell.dashboard import DashboardWindow

    before = set(QApplication.topLevelWidgets())
    window = DashboardWindow()
    try:
        QCoreApplication.processEvents()
        window._consumer.set_connected(True)
        window._consumer.apply_quiet_mode_state({"kind": "off"})
        QCoreApplication.processEvents()
        stray = [
            w for w in set(QApplication.topLevelWidgets()) - before
            if w is not window
        ]
        assert stray == [], [type(w).__name__ for w in stray]
        # The biometric card no longer carries an orphaned HRV slot.
        assert not hasattr(window._consumer, "_hrv_label")
    finally:
        window.deleteLater()


def test_overlay_is_a_non_activating_notification(qapp):
    from cortex.apps.desktop_shell.overlay import OverlayWindow
    from cortex.apps.desktop_shell.tokens import BREATHING_PACER_SIZE, POPUP_WIDTH

    win = OverlayWindow()
    try:
        assert win.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        source = inspect.getsource(OverlayWindow.show_intervention)
        source += inspect.getsource(OverlayWindow._place_as_notification)
        code_lines = [
            line for line in source.splitlines()
            if not line.strip().startswith("#")
        ]
        assert not any("activateWindow(" in line for line in code_lines)
        assert win.width() == POPUP_WIDTH
        assert win._pacer.width() == BREATHING_PACER_SIZE
        # Bare letter shortcuts are gone: every footer shortcut is Cmd-modified.
        for button in (win._snooze_btn, win._quiet_session_btn):
            shortcut = button.shortcut().toString()
            assert shortcut == "" or "Ctrl+" in shortcut, shortcut
    finally:
        win.deleteLater()


def test_overlay_pacer_only_for_breathing_plans(qapp):
    from cortex.apps.desktop_shell.overlay import OverlayWindow

    win = OverlayWindow()
    try:
        assert win._plan_is_breathing({"headline": "Close three tabs", "micro_steps": []}) is False
        assert win._plan_is_breathing({"headline": "Take a breath", "micro_steps": []}) is True
        assert win._plan_is_breathing({"breathing_pattern": "box_4_4_4_4"}) is True
        assert win._plan_is_breathing(
            {"suggested_actions": [{"action_type": "take_biology_break"}]}
        ) is True
    finally:
        win.deleteLater()


def test_escape_closes_settings_window(qapp):
    from cortex.apps.desktop_shell.settings import SettingsDialog

    dlg = SettingsDialog()
    try:
        closed: list[None] = []
        dlg.back_requested.connect(lambda: closed.append(None))
        dlg.show()
        QCoreApplication.processEvents()
        assert not dlg.isHidden()
        QTest.keyClick(dlg, Qt.Key.Key_Escape)
        QCoreApplication.processEvents()
        assert dlg.isHidden()
        assert closed == [None]
        # It is a window, not a page: no "Back" control exists.
        from PySide6.QtWidgets import QPushButton

        assert not any(
            "Back" in b.text() for b in dlg.findChildren(QPushButton)
        )
        # The cost line stays hidden until the daemon reports real spend.
        assert not dlg._budget_today_label.isVisibleTo(dlg)
    finally:
        dlg.deleteLater()


def test_escape_closes_connections_window(qapp, monkeypatch):
    from cortex.apps.desktop_shell import connections as conn

    monkeypatch.setattr(conn, "_probe_bridge", lambda _app: (False, False, "not probed"))
    monkeypatch.setattr(conn.ConnectionsPanel, "_daemon_reachable", lambda self, **kw: False)
    panel = conn.ConnectionsPanel()
    try:
        closed: list[None] = []
        panel.back_requested.connect(lambda: closed.append(None))
        panel.show()
        QCoreApplication.processEvents()
        QTest.keyClick(panel, Qt.Key.Key_Escape)
        QCoreApplication.processEvents()
        assert panel.isHidden()
        assert closed == [None]
        from PySide6.QtWidgets import QPushButton

        assert not any(
            "Back" in b.text() for b in panel.findChildren(QPushButton)
        )
        # No modal message boxes remain in the module.
        assert not hasattr(conn, "QMessageBox")
        source = inspect.getsource(conn)
        assert "xattr" not in source
    finally:
        panel.deleteLater()


def test_connections_reprobes_on_show_and_renders_inline(qapp, monkeypatch):
    from cortex.apps.desktop_shell import connections as conn

    probes: list[str] = []

    def _probe(app_name: str):
        probes.append(app_name)
        return (True, False, "")

    monkeypatch.setattr(conn, "_probe_bridge", _probe)
    monkeypatch.setattr(conn.ConnectionsPanel, "_daemon_reachable", lambda self, **kw: True)
    monkeypatch.setattr(conn, "_BROWSERS", [("Chrome", sys.executable, "chrome://extensions")])
    panel = conn.ConnectionsPanel()
    try:
        panel.show()
        QCoreApplication.processEvents()
        assert probes == ["Google Chrome"]
        status = panel.status_for("Chrome")
        assert status["rows"]["bridge"] is True
        assert status["rows"]["daemon"] is True
        checklist = panel._checklists["Chrome"]
        assert "Browser bridge registered" in checklist._rows["bridge"].text()
        assert "Cortex is running" in checklist._rows["daemon"].text()
    finally:
        panel.deleteLater()


def test_toast_floats_without_moving_content(qapp):
    from cortex.apps.desktop_shell.dashboard import DashboardWindow

    window = DashboardWindow()
    try:
        window.resize(560, 720)
        window.show()
        QCoreApplication.processEvents()
        stack_before = window._stack.geometry()
        seg_before = window._seg.geometry()

        window.show_error("Daemon unreachable", "Reconnecting in 5 s", "cid_geom")
        QCoreApplication.processEvents()

        assert window._stack.geometry() == stack_before
        assert window._seg.geometry() == seg_before
        toast = window._toast
        assert toast.parentWidget() is window
        assert not toast.isHidden()
        assert toast.mode == "error"

        window.show_info_toast("Cortex is now using your LLM", "Next suggestion uses it")
        QCoreApplication.processEvents()
        assert window._stack.geometry() == stack_before
        assert toast.mode == "info"
    finally:
        window.deleteLater()


def test_dashboard_indicators_do_not_lie_before_data(qapp):
    from cortex.apps.desktop_shell.dashboard import DashboardWindow

    window = DashboardWindow()
    try:
        consumer = window._consumer
        # Cost is hidden until the daemon reports spend.
        assert not consumer._cost_pill.isVisibleTo(consumer)
        # Session statistics wait for the first estimated state.
        assert not consumer._session_stats.isVisibleTo(consumer)
        # Extension dots carry text and accessible names.
        consumer.set_extension_connected("Chrome", True)
        assert "Chrome" in consumer._conn_labels["Chrome"].text()
        assert consumer._conn_labels["Chrome"].accessibleName()
        consumer.set_extension_connected("Chrome", False)
        assert "Off" in consumer._conn_labels["Chrome"].text()
        # The camera placeholder says what it is waiting for.
        assert "Waiting for the camera" in consumer._bio_empty_state.text()
    finally:
        window.deleteLater()


def test_onboarding_get_started_records_only_real_grants(qapp, tmp_path, monkeypatch):
    from cortex.apps.desktop_shell import onboarding as onb

    state_path = tmp_path / "onboarding_state.json"
    monkeypatch.setattr(onb, "onboarding_state_path", lambda: state_path)
    monkeypatch.setattr(onb, "check_camera_permission", lambda: False)
    monkeypatch.setattr(onb, "check_accessibility_permission", lambda: True)
    monkeypatch.setattr(onb, "_detect_continuity_camera", lambda: False)

    win = onb.OnboardingWindow()
    try:
        win._permission_timer.stop()
        finished: list[None] = []
        win.completed.connect(lambda: finished.append(None))

        # The strip already reflects the real accessibility grant.
        assert 1 in win._progress.completed_indices()
        assert 0 not in win._progress.completed_indices()

        win._on_finish()
        assert finished == [None]
        state = onb.OnboardingState.load(state_path)
        assert "accessibility" in state.completed_steps
        assert "camera" not in state.completed_steps
        assert "llm_backend" not in state.completed_steps
        assert "extensions" not in state.completed_steps
        assert "Camera" in win._finish_note.text()

        # Explicit skips are real decisions and complete their steps.
        win._on_skip_llm()
        win._on_skip_extensions()
        state = onb.OnboardingState.load(state_path)
        assert {"llm_backend", "extensions"} <= state.completed_steps
        assert {1, 2, 4} <= win._progress.completed_indices()
        assert win._llm_status.isVisibleTo(win)
        assert "offline" in win._llm_status.text().lower()

        # Notifications success is a status row, not a disabled button.
        win._apply_notification_auth_result(True)
        assert win._notif_status_ref.isVisibleTo(win)
        assert not win._notif_btn_ref.isVisibleTo(win)
    finally:
        win.deleteLater()


def test_tray_status_reflects_evidence(qapp):
    from cortex.apps.desktop_shell import tray as tray_mod

    tray = tray_mod.CortexTrayIcon(qapp)
    tray.update_state("HYPER", 0.9, "insufficient_evidence", 0.2)
    assert tray._state_action.text() == "Status: Not enough evidence"
    tray.update_state("HYPER", 0.9, "warming_up", 0.4)
    assert tray._state_action.text() == "Status: Still gathering"
    tray.update_state("FLOW", 0.9, "estimated", 0.9)
    assert tray._state_action.text().startswith("Status: ")
    assert "FLOW" not in tray._state_action.text()
    tray.set_paused(True)
    assert "FLOW" not in tray.toolTip()
    assert "Paused" in tray.toolTip()
