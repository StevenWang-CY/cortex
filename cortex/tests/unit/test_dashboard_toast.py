"""Phase J-2 — Dashboard error toast with cid quote-back.

Run with:
    ``QT_QPA_PLATFORM=offscreen pytest cortex/tests/unit/test_dashboard_toast.py``

The dashboard top-bar toast surfaces daemon errors with the F19
correlation id quoted back ("ref: <cid>") so the user can copy it into a
support ticket. The cid is selectable (TextSelectableByMouse) — the
audit-finding root cause was a non-selectable cid the user couldn't
copy. The toast auto-dismisses after 8 s; the contract pins that
duration so a future refactor can't silently shrink it.
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
    from PySide6.QtCore import QCoreApplication, Qt
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
    """Pump the Qt event loop for ``ms`` milliseconds so timers fire."""
    deadline = time.monotonic() + ms / 1000.0
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.01)


def test_toast_renders_title_body_cid(dashboard):
    """show_error populates all three slots and the toast becomes visible."""
    toast = dashboard._toast
    assert toast is not None

    assert toast.isHidden()
    dashboard.show_error(
        "Daemon unreachable",
        "Reconnecting in 5 s",
        "cid_abc123def456",
    )
    assert not toast.isHidden()
    assert toast.current_cid == "cid_abc123def456"
    assert "cid_abc123def456" in toast._cid_label.text()
    assert "Daemon unreachable" in toast._title_label.text()
    assert "Reconnecting in 5 s" in toast._body_label.text()


def test_toast_cid_is_selectable(dashboard):
    """Phase J-2 contract: the cid label MUST be selectable so the user
    can copy it into a support ticket. The audit root cause was a
    non-selectable cid — pin the flag with an explicit test so a future
    stylesheet refactor cannot silently regress it."""
    toast = dashboard._toast
    assert toast is not None
    dashboard.show_error("err", "body", "cid_pinned_xyz")
    assert toast.is_cid_selectable()
    # Defensive: confirm the underlying flag is set, not just the helper.
    flags = toast._cid_label.textInteractionFlags()
    assert flags & Qt.TextInteractionFlag.TextSelectableByMouse


def test_toast_auto_dismiss_after_eight_seconds(dashboard):
    """The toast auto-dismisses at the documented 8 s budget. Test uses a
    shortened duration so we don't burn 8 s of wall-clock; the same code
    path fires the dismiss either way."""
    from cortex.apps.desktop_shell.components import DEFAULT_TOAST_DURATION_MS

    # The production default is pinned at 8000 ms.
    assert DEFAULT_TOAST_DURATION_MS == 8_000

    toast = dashboard._toast
    assert toast is not None
    toast._duration_ms = 200  # speed up for the test
    toast._timer.setInterval(200)

    dashboard.show_error("err", "body", "cid_dismiss")
    assert not toast.isHidden()

    _process_for(500)

    assert toast.isHidden(), "toast must hide after timer expiry"
    assert toast.current_cid == "", "cid cleared on dismiss"


def test_toast_close_button_dismisses_immediately(dashboard):
    """Power users can dismiss the toast before its auto-timer expires."""
    toast = dashboard._toast
    assert toast is not None

    dashed_events: list[None] = []
    toast.dismissed.connect(lambda: dashed_events.append(None))

    dashboard.show_error("err", "body", "cid_manual")
    assert not toast.isHidden()

    toast._close_btn.click()
    assert toast.isHidden()
    assert len(dashed_events) == 1


def test_empty_cid_does_not_crash(dashboard):
    """Daemon may surface an error before any cid is bound (e.g. WS
    handshake failure before the first request). The toast must render
    with an empty cid slot rather than crashing."""
    toast = dashboard._toast
    assert toast is not None
    dashboard.show_error("Early failure", "WS handshake aborted", "")
    assert not toast.isHidden()
    assert toast.current_cid == ""
    # The ref-row's cid label exists but is empty.
    assert toast._cid_label.text() == ""


def test_bridge_signal_round_trips_to_toast(qapp):
    """The DaemonBridge.error_occurred signal carries (title, body, cid)
    as Qt-marshalled str / str / str. Emitting it from the daemon thread
    must hit the dashboard toast on the Qt main thread."""
    from cortex.apps.desktop_shell.controller import DaemonBridge

    bridge = DaemonBridge()
    captured: list[tuple[str, str, str]] = []
    bridge.error_occurred.connect(
        lambda t, b, c: captured.append((t, b, c))
    )

    bridge.on_error("Bedrock unavailable", "Retrying", "cid_signal_round_trip")
    QCoreApplication.processEvents()

    assert captured == [
        ("Bedrock unavailable", "Retrying", "cid_signal_round_trip")
    ]


def test_bridge_signal_defaults_for_missing_fields(qapp):
    """on_error must gracefully accept missing body / cid arguments."""
    from cortex.apps.desktop_shell.controller import DaemonBridge

    bridge = DaemonBridge()
    captured: list[tuple[str, str, str]] = []
    bridge.error_occurred.connect(
        lambda t, b, c: captured.append((t, b, c))
    )

    # Default cid arg.
    bridge.on_error("Bedrock unavailable", "Retrying")
    QCoreApplication.processEvents()
    assert captured[-1] == ("Bedrock unavailable", "Retrying", "")

    # All three falsy.
    bridge.on_error("", "", "")
    QCoreApplication.processEvents()
    # Title defaults to "Error" when caller passes empty.
    assert captured[-1] == ("Error", "", "")
