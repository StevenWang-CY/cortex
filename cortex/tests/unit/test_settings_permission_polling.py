"""SettingsDialog — live macOS permission polling.

The Settings dialog surfaces the current TCC / AX grant state for the
camera and accessibility permissions, polled every 1.5s while the
dialog is visible. Pre-fix only the onboarding wizard polled these
flags; users who toggled the grant in System Settings mid-session had
no in-app feedback until they relaunched Cortex.

Run with:
    ``QT_QPA_PLATFORM=offscreen pytest cortex/tests/unit/test_settings_permission_polling.py``
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Same PySide6-vs-stub guard the empty-state test uses — keeps the real
# Qt module loaded when available and aborts the test cleanly on a
# Linux-CI runner without Qt.
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
def settings_dialog(qapp, monkeypatch):
    """Build a SettingsDialog with the macOS permission probes stubbed
    to return ``False`` initially. Tests then flip the stubs and
    invoke ``_refresh_permission_states`` to assert the UI updates.

    SettingsDialog imports the helpers via the re-export at
    ``cortex.libs.utils``; monkeypatching the bottom-level module
    (``cortex.libs.utils.platform``) is not sufficient because the
    package-level binding was captured at ``cortex/libs/utils/__init__.py``
    import time. We patch both layers so callers find the stub
    regardless of how they imported it.
    """
    from cortex.apps.desktop_shell import mac_native
    from cortex.libs import utils as _utils
    from cortex.libs.utils import platform as _platform

    monkeypatch.setattr(mac_native, "apply_vibrancy", lambda *a, **kw: False)
    monkeypatch.setattr(
        mac_native, "apply_unified_titlebar", lambda *a, **kw: False
    )

    # Default both probes off. Individual tests flip them to True via
    # the helper below so the package- and submodule-level bindings
    # always agree.
    def _set(cam: bool, acc: bool) -> None:
        monkeypatch.setattr(_platform, "check_camera_permission", lambda: cam)
        monkeypatch.setattr(_utils, "check_camera_permission", lambda: cam)
        monkeypatch.setattr(
            _platform, "check_accessibility_permission", lambda: acc
        )
        monkeypatch.setattr(
            _utils, "check_accessibility_permission", lambda: acc
        )

    _set(False, False)

    from cortex.apps.desktop_shell.settings import SettingsDialog

    dlg = SettingsDialog()
    try:
        yield dlg, _set
    finally:
        try:
            dlg.deleteLater()
        except RuntimeError:
            pass


def test_initial_render_reports_not_granted_for_both(settings_dialog):
    dlg, _ = settings_dialog
    cam_label = dlg._camera_perm_row["label"]
    acc_label = dlg._accessibility_perm_row["label"]

    # Both rows should default to "not granted" copy. The exact CTA
    # text varies between the HTML link form and the plain form; check
    # the substring that's stable.
    assert "Camera access not granted" in cam_label.text()
    assert "Accessibility access not granted" in acc_label.text()


def test_refresh_flips_to_granted_when_probe_returns_true(settings_dialog):
    dlg, set_perms = settings_dialog

    # User opens System Settings, flips Camera ON.
    set_perms(True, False)
    dlg._refresh_permission_states()

    cam_label = dlg._camera_perm_row["label"]
    acc_label = dlg._accessibility_perm_row["label"]
    assert "Camera access granted" in cam_label.text()
    assert dlg._camera_perm_row["granted"] is True
    assert "Accessibility access not granted" in acc_label.text()
    assert dlg._accessibility_perm_row["granted"] is False


def test_refresh_flips_back_when_probe_revokes(settings_dialog):
    dlg, set_perms = settings_dialog

    set_perms(True, False)
    dlg._refresh_permission_states()
    assert dlg._camera_perm_row["granted"] is True

    # User revokes Camera in System Settings.
    set_perms(False, False)
    dlg._refresh_permission_states()
    assert dlg._camera_perm_row["granted"] is False
    assert "Camera access not granted" in dlg._camera_perm_row["label"].text()


def test_refresh_is_idempotent_no_repeat_writes(settings_dialog):
    """The guard inside ``_render_permission_row`` skips Qt writes when
    the polled state is unchanged. Smoke-checked by confirming that
    polling twice with the same value leaves ``granted`` stable and
    setText is not called by the second invocation.
    """
    dlg, set_perms = settings_dialog

    set_perms(False, True)
    dlg._refresh_permission_states()
    label_after_first = dlg._accessibility_perm_row["label"].text()
    granted_after_first = dlg._accessibility_perm_row["granted"]

    # Patch setText so a second invocation that ignores the cache would
    # be visible. The render guard should prevent the call.
    write_count = {"n": 0}
    orig_set = dlg._accessibility_perm_row["label"].setText

    def _counting_set(text):  # type: ignore[no-untyped-def]
        write_count["n"] += 1
        orig_set(text)

    dlg._accessibility_perm_row["label"].setText = _counting_set  # type: ignore[assignment]
    dlg._refresh_permission_states()

    assert write_count["n"] == 0
    assert dlg._accessibility_perm_row["granted"] is granted_after_first
    assert dlg._accessibility_perm_row["label"].text() == label_after_first


def test_probe_exception_does_not_crash_refresh(
    settings_dialog, monkeypatch
):
    """If the platform helper raises (e.g. Cocoa bridge surprise), the
    poll should treat the permission as not-granted rather than
    propagating the exception and freezing the timer.
    """
    dlg, set_perms = settings_dialog

    from cortex.libs import utils as _utils
    from cortex.libs.utils import platform as _platform

    def _boom():
        raise RuntimeError("synthetic AVCaptureDevice failure")

    monkeypatch.setattr(_platform, "check_camera_permission", _boom)
    monkeypatch.setattr(_utils, "check_camera_permission", _boom)
    monkeypatch.setattr(
        _platform, "check_accessibility_permission", lambda: True
    )
    monkeypatch.setattr(
        _utils, "check_accessibility_permission", lambda: True
    )

    # Must not raise — the wrapper swallows probe exceptions.
    dlg._refresh_permission_states()
    assert dlg._camera_perm_row["granted"] is False
    assert dlg._accessibility_perm_row["granted"] is True


def test_open_system_settings_invokes_open_with_deep_link(
    settings_dialog,
    monkeypatch,
):
    """Clicking the not-granted CTA should shell out to ``open
    x-apple.systempreferences:...``. We don't actually want to launch
    System Settings during the test, so patch subprocess.Popen and
    verify it received the right URL for each target.
    """
    dlg, _ = settings_dialog
    calls: list[list[str]] = []

    class _FakePopen:
        def __init__(self, args, **_kw):  # type: ignore[no-untyped-def]
            calls.append(list(args))

    import subprocess

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    dlg._open_system_settings("camera")
    dlg._open_system_settings("accessibility")
    dlg._open_system_settings("nonsense")  # ignored — unknown target

    assert len(calls) == 2
    assert calls[0] == [
        "open",
        "x-apple.systempreferences:com.apple.preference.security?Privacy_Camera",
    ]
    assert calls[1] == [
        "open",
        "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    ]


def test_permission_timer_started_on_construction(settings_dialog):
    dlg, _ = settings_dialog
    # The QTimer should be created and running after __init__ so polling
    # begins immediately, not 1.5s after the first showEvent.
    assert dlg._permission_timer is not None
    assert dlg._permission_timer.interval() == 1500
    assert dlg._permission_timer.isActive()
