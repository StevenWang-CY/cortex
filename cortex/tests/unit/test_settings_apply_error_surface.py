"""P1-14 — Settings apply errors surfaced via QMessageBox.

The apply handlers (apply_payload, apply_cost_response,
apply_provider_test_result) previously swallowed all exceptions silently
with a logger.debug call. P1-14 replaces that with a user-visible
QMessageBox.warning (rate-limited to 1/s) and a WARNING-level log.

Run with: ``QT_QPA_PLATFORM=offscreen pytest cortex/tests/unit/test_settings_apply_error_surface.py``
"""

from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

for _name in list(sys.modules):
    if _name == "PySide6" or _name.startswith("PySide6."):
        mod = sys.modules[_name]
        if not hasattr(mod, "__file__") or "site-packages" not in str(
            getattr(mod, "__file__", "") or ""
        ):
            del sys.modules[_name]

import pytest  # noqa: E402

try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    pytest.skip("PySide6 not available", allow_module_level=True)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def dialog(qapp, monkeypatch):
    from cortex.apps.desktop_shell import mac_native
    from cortex.apps.desktop_shell import settings as settings_mod

    monkeypatch.setattr(mac_native, "apply_vibrancy", lambda *a, **kw: False)
    monkeypatch.setattr(
        mac_native, "apply_unified_titlebar", lambda *a, **kw: False
    )

    d = settings_mod.SettingsDialog()
    yield d
    try:
        d.deleteLater()
    except RuntimeError:
        pass


# ---------------------------------------------------------------------------
# Test 1: apply_payload with a broken widget raises QMessageBox.warning
# ---------------------------------------------------------------------------

def test_apply_payload_error_surfaces_qmessagebox(dialog, monkeypatch):
    """When apply_payload crashes (broken widget), QMessageBox.warning fires once."""
    from PySide6.QtWidgets import QMessageBox

    calls: list = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        staticmethod(lambda *a, **kw: calls.append(a)),
    )

    # Break the quiet_mode widget so setChecked raises.
    original = dialog._quiet_mode
    class _BrokenCheckBox:
        def setChecked(self, _v: bool) -> None:
            raise RuntimeError("simulated widget crash")
    dialog._quiet_mode = _BrokenCheckBox()  # type: ignore[assignment]

    try:
        dialog.apply_payload({"quiet_mode": True})
    except Exception:
        pass  # apply_payload must not re-raise

    dialog._quiet_mode = original

    assert len(calls) == 1, (
        f"Expected QMessageBox.warning called once, got {len(calls)}"
    )


# ---------------------------------------------------------------------------
# Test 2: rate-limit — two rapid failures show only one dialog
# ---------------------------------------------------------------------------

def test_apply_error_rate_limited_to_one_per_second(dialog, monkeypatch):
    """Two failures within 1 second only open one dialog."""
    from PySide6.QtWidgets import QMessageBox

    calls: list = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        staticmethod(lambda *a, **kw: calls.append(a)),
    )

    # Force the timestamp to be far in the past so the first call goes through.
    dialog._last_error_dialog_ts = 0.0

    dialog._show_apply_error("first failure")
    dialog._show_apply_error("second failure — must be swallowed")

    assert len(calls) == 1, (
        f"Rate limiter must suppress the second dialog; got {len(calls)} calls"
    )


# ---------------------------------------------------------------------------
# Test 3: after 1 second the guard resets and a second dialog can show
# ---------------------------------------------------------------------------

def test_apply_error_allows_new_dialog_after_cooldown(dialog, monkeypatch):
    """After the 1-second cooldown a new error dialog is shown."""
    from PySide6.QtWidgets import QMessageBox

    calls: list = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        staticmethod(lambda *a, **kw: calls.append(a)),
    )

    dialog._last_error_dialog_ts = 0.0
    dialog._show_apply_error("first error")
    assert len(calls) == 1

    # Wind back the timestamp > 1 s so the guard expires.
    dialog._last_error_dialog_ts = time.monotonic() - 1.5
    dialog._show_apply_error("second error after cooldown")
    assert len(calls) == 2
