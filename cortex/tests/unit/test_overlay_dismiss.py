"""F06 — Overlay timer cleanup + idempotent dismiss.

These tests exercise the real Qt widget (offscreen platform) to prove the
dismiss path is now idempotent: the first dismiss (user or auto) wins, and
the timer is stopped unconditionally so it cannot fire against a hidden or
destroyed widget.

We construct the overlay but avoid the macOS-only ``apply_vibrancy`` /
``apply_unified_titlebar`` calls (they crash under the offscreen Qt
platform because ``winId()`` returns a non-NSView pointer); we directly
invoke ``_timeout_timer.start()`` to simulate the showing-state without
needing a real window.

Run with: ``QT_QPA_PLATFORM=offscreen pytest cortex/tests/unit/test_overlay_dismiss.py``
"""

from __future__ import annotations

import os
import sys


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Ensure no stale PySide6 mocks remain from other tests in the same session.
for _name in list(sys.modules):
    if _name == "PySide6" or _name.startswith("PySide6."):
        mod = sys.modules[_name]
        # Heuristic: real PySide6 modules have a known C-extension marker.
        if not hasattr(mod, "__file__") or "site-packages" not in str(
            getattr(mod, "__file__", "") or ""
        ):
            del sys.modules[_name]

import pytest  # noqa: E402

try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - environment misconfig
    pytest.skip("PySide6 not available", allow_module_level=True)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def overlay(qapp, monkeypatch):
    # Stub the macOS-only AppKit calls — offscreen Qt has no real NSWindow.
    from cortex.apps.desktop_shell import mac_native, overlay as overlay_mod

    monkeypatch.setattr(mac_native, "apply_vibrancy", lambda *a, **kw: False)
    monkeypatch.setattr(
        mac_native, "apply_unified_titlebar", lambda *a, **kw: False
    )

    w = overlay_mod.OverlayWindow()
    # Simulate the "shown with active intervention" state without calling
    # the macOS-bound show() codepath.
    w._intervention_id = "iv-test"
    w._dismissed = False
    w._timeout_timer.start()
    yield w
    try:
        w._timeout_timer.stop()
        w.deleteLater()
    except RuntimeError:
        pass


def _collect_emissions(widget):
    received: list[str] = []
    widget.dismissed.connect(lambda iv_id: received.append(iv_id))
    return received


def test_double_user_dismiss_emits_once(overlay):
    received = _collect_emissions(overlay)

    overlay._user_dismiss()
    overlay._user_dismiss()

    assert received == ["iv-test"], (
        f"double user-dismiss should emit once, got {received}"
    )
    assert overlay._dismissed is True
    assert not overlay._timeout_timer.isActive()


def test_auto_then_user_emits_once(overlay):
    received = _collect_emissions(overlay)

    overlay._auto_dismiss()
    overlay._user_dismiss()

    assert received == ["iv-test"], (
        f"auto then user should emit once, got {received}"
    )
    assert overlay._dismissed is True


def test_user_then_auto_emits_once(overlay):
    received = _collect_emissions(overlay)

    overlay._user_dismiss()
    overlay._auto_dismiss()

    assert received == ["iv-test"], (
        f"user then auto should emit once, got {received}"
    )
    assert overlay._dismissed is True


def test_widget_destroyed_mid_timer_no_emission(qapp, monkeypatch):
    """Tearing the widget down via closeEvent + deleteLater stops the timer
    and marks the overlay dismissed so the auto-dismiss slot cannot fire
    against a partially-collected Qt object."""
    from cortex.apps.desktop_shell import mac_native, overlay as overlay_mod

    monkeypatch.setattr(mac_native, "apply_vibrancy", lambda *a, **kw: False)
    monkeypatch.setattr(
        mac_native, "apply_unified_titlebar", lambda *a, **kw: False
    )

    w = overlay_mod.OverlayWindow()
    w._intervention_id = "iv-mid"
    w._dismissed = False
    w._timeout_timer.start()

    received: list[str] = []
    w.dismissed.connect(lambda iv_id: received.append(iv_id))

    assert w._timeout_timer.isActive(), "timer should be active"

    # Simulate window close (dashboard-driven teardown). This must stop the
    # timer so the auto-dismiss slot cannot fire afterwards.
    w.close()
    assert not w._timeout_timer.isActive(), (
        "timer must be stopped after closeEvent"
    )
    assert w._dismissed is True

    # If the auto-dismiss slot did somehow get invoked (e.g. queued before
    # the close), the idempotency guard must suppress the emission.
    w._auto_dismiss()
    assert received == [], (
        f"no emission expected after teardown, got {received}"
    )

    w.deleteLater()
