"""Rendered layout and proposal lifetime regressions; no camera or user profile."""
from __future__ import annotations

import math
import os
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QWidget

from cortex.apps.desktop_shell.dashboard import DashboardWindow
from cortex.apps.desktop_shell.suggestion_review import SuggestionReview
from cortex.apps.desktop_shell.view_models import advanced_state_view, consumer_state_view


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def sandbox_native_surfaces(monkeypatch):
    from cortex.apps.desktop_shell import mac_native

    monkeypatch.setattr(mac_native, "apply_vibrancy", lambda *a, **kw: False)
    monkeypatch.setattr(mac_native, "apply_unified_titlebar", lambda *a, **kw: False)
    monkeypatch.setattr("cortex.libs.utils.check_camera_permission", lambda: False)
    monkeypatch.setattr("cortex.libs.utils.check_accessibility_permission", lambda: False)


@pytest.fixture
def dashboard(app, monkeypatch):
    from cortex.apps.desktop_shell import mac_native
    monkeypatch.setattr(mac_native, "apply_vibrancy", lambda *a, **kw: False)
    monkeypatch.setattr(mac_native, "apply_unified_titlebar", lambda *a, **kw: False)
    window = DashboardWindow()
    yield window
    window.hide()
    window.deleteLater()


def test_all_status_controls_fit_at_minimum_width(dashboard, app):
    dashboard.resize(380, 500)
    tab = dashboard._consumer
    tab.update_state({"state": "FLOW", "status": "estimated", "biometrics": {"heart_rate": 72, "blink_rate": 12}})
    tab.apply_auto_focus_state(True, "developer")
    tab._restore_pill.show()
    tab._break_pill.show()
    tab._baseline_pill.setText("Recalibrate profile")
    tab._baseline_pill.show()
    tab.apply_cost_update(1.25, 5.0)
    dashboard.show()
    for _ in range(3):
        app.processEvents()
    assert dashboard.width() == 380
    assert tab._scroll.horizontalScrollBar().maximum() == 0
    chips = [tab._meta_strip.itemAt(i).widget() for i in range(tab._meta_strip.count())]
    for chip in chips:
        assert chip.width() >= chip.sizeHint().width(), chip.text()
    assert len({chip.y() for chip in chips}) > 1
    for label in tab._conn_labels.values():
        assert label.width() >= label.sizeHint().width(), label.text()
    assert tab._connect_btn.width() >= tab._connect_btn.sizeHint().width()
    # Session action remains reachable without scrolling the content.
    assert tab._stop_btn.isVisible()
    assert tab._stop_btn.mapTo(dashboard, tab._stop_btn.rect().bottomRight()).y() < dashboard.height()


def test_disconnect_and_reconnect_require_fresh_measurements(dashboard):
    tab = dashboard._consumer
    payload = {"status": "estimated", "state": "FLOW", "biometrics": {"heart_rate": 72, "blink_rate": 12}}
    tab.update_state(payload)
    assert tab._bpm_label.text() == "72"
    tab.set_connected(False)
    assert tab._bpm_label.text() == "--"
    assert not tab._bio_numerics.isVisibleTo(tab)
    assert "offline" in tab._bio_status_label.text()
    tab.set_connected(True)
    assert not tab._bio_numerics.isVisibleTo(tab)
    assert "fresh" in tab._bio_status_label.text()
    tab.update_state(payload)
    assert tab._bio_numerics.isVisibleTo(tab)


@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan, "bad", True, -1])
def test_invalid_measurements_never_become_zero_or_infinite(value):
    payload = {"biometrics": {"heart_rate": value, "blink_rate": value}, "support_scores": {"flow_like": value}}
    view = consumer_state_view(payload)
    assert view.heart_rate is None
    assert view.blink_rate is None
    assert math.isfinite(advanced_state_view(payload).scores["flow"])


def test_stale_capture_cannot_publish_a_cached_reading():
    view = consumer_state_view({"capture": {"stale": True, "frames_flowing": False}, "biometrics": {"heart_rate": 72, "blink_rate": 10}})
    assert view.heart_rate is None and view.blink_rate is None
    assert "Camera offline" in view.biometrics_status


class ReviewDashboard(QWidget):
    review_suggestion_requested = Signal()
    daemon_stop_requested = Signal()

    def set_pending_suggestion(self, payload):
        self.pending = payload

    def record_intervention_seen(self):
        self.shown_count = getattr(self, "shown_count", 0) + 1


class ReviewOverlay(QObject):
    dismissed = Signal(str)
    action_invoked = Signal(str, dict)

    def __init__(self):
        super().__init__()
        self.presented: list[dict[str, Any]] = []
        self.focused = 0
        self.suppressed = 0

    def show_intervention(self, payload):
        self.presented.append(payload)

    def isVisible(self):
        return bool(self.presented)

    def focus_for_review(self):
        self.focused += 1

    def suppress(self):
        self.suppressed += 1


@pytest.fixture
def review(app, monkeypatch):
    from cortex.apps.desktop_shell import suggestion_review
    clock = [100.0]
    monkeypatch.setattr(suggestion_review.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(suggestion_review.time, "time", lambda: clock[0])
    dashboard = ReviewDashboard()
    overlay = ReviewOverlay()
    presenter = SuggestionReview(dashboard, overlay)
    yield presenter, dashboard, overlay, clock
    presenter.clear()
    dashboard.deleteLater()


def test_review_opens_exact_pending_payload_only_after_explicit_gesture(review):
    presenter, dashboard, overlay, _ = review
    payload = {"intervention_id": "i1", "headline": "One next step", "suggested_actions": [{"action_id": "a1"}]}
    presenter.present(payload, show_immediately=False)
    payload["suggested_actions"][0]["action_id"] = "changed-after-delivery"
    assert not overlay.presented and overlay.focused == 0
    assert dashboard.pending["intervention_id"] == "i1"
    dashboard.review_suggestion_requested.emit()
    assert overlay.presented[0]["suggested_actions"][0]["action_id"] == "a1"
    assert overlay.focused == 1


def test_repeated_update_and_review_do_not_extend_expiry(review):
    presenter, dashboard, overlay, clock = review
    payload = {"intervention_id": "i1", "action_manifest": {"expires_at_unix_ms": 110_000}}
    presenter.present(payload, show_immediately=True)
    assert overlay.focused == 0
    clock[0] = 109
    presenter.present(payload, show_immediately=False)
    assert presenter._deadline == 110
    clock[0] = 111
    dashboard.review_suggestion_requested.emit()
    assert dashboard.pending is None and overlay.focused == 0
    # A delayed micro-step update must not reopen a retired suggestion.
    presenter.present({"intervention_id": "i1"}, show_immediately=True)
    assert dashboard.pending is None


def test_updates_and_review_count_one_visible_nudge(review):
    presenter, dashboard, _, _ = review
    payload = {"intervention_id": "i1"}
    presenter.present(payload, show_immediately=False)
    assert not hasattr(dashboard, "shown_count")
    presenter.review()
    presenter.present(payload, show_immediately=True)
    presenter.review()
    assert dashboard.shown_count == 1


@pytest.mark.parametrize("event", ["dismiss", "action", "stop", "transaction", "quiet"])
def test_terminal_events_clear_pending_review(review, event):
    presenter, dashboard, overlay, _ = review
    presenter.present({"intervention_id": "i1"}, show_immediately=False)
    if event == "dismiss":
        overlay.dismissed.emit("i1")
    elif event == "action":
        overlay.action_invoked.emit("i1", {})
    elif event == "stop":
        dashboard.daemon_stop_requested.emit()
    elif event == "transaction":
        presenter.transaction_updated({"intervention_id": "i1", "state": "applied"})
    else:
        presenter.quiet_updated({"kind": "pause"})
    assert dashboard.pending is None
    dashboard.review_suggestion_requested.emit()
    assert overlay.focused == 0


def test_old_dismissal_does_not_clear_replacement_suggestion(review):
    presenter, dashboard, overlay, _ = review
    presenter.present({"intervention_id": "old"}, show_immediately=False)
    presenter.present({"intervention_id": "new"}, show_immediately=False)
    overlay.dismissed.emit("old")
    assert dashboard.pending["intervention_id"] == "new"


def test_ws_quiet_message_clears_review_and_updates_dashboard(dashboard, app):
    from cortex.apps.desktop_shell.main import WebSocketBridge

    overlay = ReviewOverlay()
    presenter = SuggestionReview(dashboard, overlay)
    bridge = WebSocketBridge()
    bridge.quiet_mode_state_received.connect(presenter.quiet_updated)
    bridge.quiet_mode_state_received.connect(dashboard.apply_quiet_mode_state)
    presenter.present({"intervention_id": "ws-1"}, show_immediately=False)
    assert bridge._message_router.dispatch("QUIET_MODE_STATE", {"kind": "pause"})
    assert presenter._payload is None
    assert dashboard._consumer._quiet_capsule.text() == "Paused"


def test_settings_no_horizontal_scroll_in_external_mode(app, monkeypatch, tmp_path):
    from PySide6.QtCore import QSettings

    from cortex.apps.desktop_shell import settings
    monkeypatch.setattr(settings, "QSettings", lambda *a: QSettings(str(tmp_path / "prefs.ini"), QSettings.Format.IniFormat))
    monkeypatch.setattr(settings.SettingsDialog, "_refresh_permission_states", lambda self: None)
    window = settings.SettingsDialog()
    try:
        window.resize(440, 520)
        window._llm_privacy_mode.setCurrentIndex(2)
        window._schedule_disclosure._header.setChecked(True)
        window.show()
        app.processEvents()
        assert window._scroll_area.horizontalScrollBar().maximum() == 0
        assert window._baseline_freshness_label.width() > 100
    finally:
        window.hide()
        window.deleteLater()


def test_onboarding_finish_is_outside_scroll_and_visible_at_short_height(app, monkeypatch):
    from cortex.apps.desktop_shell import onboarding
    monkeypatch.setattr(onboarding, "check_camera_permission", lambda: False)
    monkeypatch.setattr(onboarding, "check_accessibility_permission", lambda: False)
    monkeypatch.setattr(onboarding.OnboardingWindow, "_start_continuity_probe", lambda self: None)
    monkeypatch.setattr(onboarding.OnboardingWindow, "_refresh_permission_states", lambda self: None)
    monkeypatch.setattr("cortex.libs.utils.secrets.get_password_safe", lambda *a, **kw: None)
    window = onboarding.OnboardingWindow()
    try:
        window.resize(600, 520)
        window.show()
        app.processEvents()
        button = window._finish_btn_ref
        before = button.mapTo(window, button.rect().topLeft())
        window._scroll_area.verticalScrollBar().setValue(window._scroll_area.verticalScrollBar().maximum())
        app.processEvents()
        assert button.mapTo(window, button.rect().topLeft()) == before
        assert 0 <= before.y() < window.height() - button.height()
    finally:
        window.hide()
        window.deleteLater()


def test_retired_overlay_rejects_a_queued_action_click(app):
    from cortex.apps.desktop_shell.overlay import OverlayWindow

    overlay = OverlayWindow()
    try:
        overlay._intervention_id = "finished"
        overlay._execution_mode = "authorized"
        observed = []
        overlay.action_invoked.connect(lambda *args: observed.append(args))
        overlay.retire_suggestion()
        overlay._on_action_clicked({"action_id": "late-click"})
        assert not observed
        assert overlay._intervention_id == ""
    finally:
        overlay.deleteLater()
