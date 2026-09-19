"""The shell's window lifecycle: close is not quit, and preferences persist.

Four defects from the v0.5.0 audit cycle, all in the desktop shell, all
sharing the property that the user's action and the app's response disagreed:

* ``desktop-shell:751`` — the dashboard's red close button ended the session
  and quit Cortex, contradicting ``setQuitOnLastWindowClosed(False)`` and
  leaving the tray's own "Dashboard" item pointing at nothing.
* ``desktop-shell:1596`` — the colour-blind palette applied live but was
  written only by Apply, so it reverted at the next launch.
* ``desktop-shell:2353`` — a failed session export was logged at WARNING and
  nowhere else, so it was indistinguishable from a successful one.
* ``desktop-shell:311`` — covered by
  ``test_settings_permission_polling.py``; kept here only as a pointer.

Run with:
    ``QT_QPA_PLATFORM=offscreen pytest cortex/tests/unit/test_shell_window_lifecycle.py``
"""

from __future__ import annotations

import os
import sys

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
def _no_native_chrome(monkeypatch):
    from cortex.apps.desktop_shell import mac_native

    monkeypatch.setattr(mac_native, "apply_vibrancy", lambda *a, **kw: False)
    monkeypatch.setattr(
        mac_native, "apply_unified_titlebar", lambda *a, **kw: False
    )


# ---------------------------------------------------------------------------
# desktop-shell:751 — close puts the window away; it does not quit Cortex
# ---------------------------------------------------------------------------


def test_closing_the_dashboard_hides_it_instead_of_quitting(
    qapp, _no_native_chrome,
):
    """The red close button must leave Cortex running in the menu bar.

    ``QApplication.lastWindowClosed`` fires on any close of the last visible
    primary window — the ordinary macOS red close button included, and
    regardless of ``setQuitOnLastWindowClosed(False)``. Wiring it to the
    user-quit path meant closing the dashboard armed a two-phase stop with
    ``quit_after=True`` and terminated the app. On macOS the red button and
    Cmd+W are the same gesture, so there was no way to put the window away
    without ending the session, and the tray's "Dashboard" item — the whole
    point of a menu-bar app — could never bring anything back.
    """
    from cortex.apps.desktop_shell.dashboard import DashboardWindow

    win = DashboardWindow()
    try:
        win.show()
        assert win.isVisible()

        closed_for_real = win.close()

        assert closed_for_real is False, (
            "an ordinary close must be refused, not accepted"
        )
        assert not win.isVisible(), "the window should be hidden"
        # Still a live widget the tray can re-show.
        assert win.isVisibleTo(win.parentWidget()) is False
        win.show()
        assert win.isVisible()
    finally:
        try:
            win.close_for_quit()
            win.deleteLater()
        except RuntimeError:
            pass


def test_teardown_can_still_close_the_dashboard_for_real(
    qapp, _no_native_chrome,
):
    """``close_for_quit`` is the one caller entitled to a genuine close."""
    from cortex.apps.desktop_shell.dashboard import DashboardWindow

    win = DashboardWindow()
    try:
        win.show()
        win.close_for_quit()
        assert not win.isVisible()
    finally:
        try:
            win.deleteLater()
        except RuntimeError:
            pass


def test_the_quit_event_routes_through_the_recap_flow(qapp):
    """Quit is hooked at ``QEvent.Quit``, which is cancellable.

    ``aboutToQuit`` is not, which is why the recap routing cannot live there;
    ``lastWindowClosed`` is cancellable but fires for closes that are not
    quits, which is the defect. ``QEvent.Quit`` is what Cmd+Q and the app
    menu's Quit deliver, and it is neither.
    """
    from PySide6.QtCore import QEvent

    from cortex.apps.desktop_shell.controller import _QuitEventFilter

    calls: list[None] = []
    filt = _QuitEventFilter(lambda: calls.append(None))

    consumed = filt.eventFilter(qapp, QEvent(QEvent.Type.Quit))

    assert calls == [None]
    assert consumed is True, (
        "the event must be consumed; letting Qt also quit races the flow"
    )

    # Every other event passes straight through.
    assert filt.eventFilter(qapp, QEvent(QEvent.Type.Show)) is False
    assert calls == [None]


# ---------------------------------------------------------------------------
# desktop-shell:1596 — an accessibility choice that applies live, persists
# ---------------------------------------------------------------------------


def test_choosing_a_palette_persists_it_without_clicking_apply(
    qapp, _no_native_chrome,
):
    """The live recolour is the cue that says no Apply is needed.

    ``_on_palette_changed`` swapped the runtime palette and emitted
    ``palette_changed`` the instant the combo changed — the dashboard
    recoloured on the spot — but the value only reached QSettings through
    ``_persist_settings`` inside ``_apply_settings``. Closing Settings
    without clicking Apply silently reverted a colour-blind palette at the
    next launch, and nothing on screen suggested Apply was required.
    """
    from cortex.apps.desktop_shell.settings import SettingsDialog

    dlg = SettingsDialog()
    try:
        deuteranopia = next(
            index
            for index in range(dlg._palette_combo.count())
            if dlg._palette_combo.itemData(index) == "deuteranopia"
        )
        dlg._palette_combo.setCurrentIndex(deuteranopia)
        dlg._on_palette_changed(deuteranopia)

        # Written now — not at Apply.
        assert dlg._qs.value("palette_variant") == "deuteranopia"
    finally:
        try:
            dlg._qs.setValue("palette_variant", "default")
            dlg._qs.sync()
            dlg.deleteLater()
        except RuntimeError:
            pass


# ---------------------------------------------------------------------------
# desktop-shell:2353 — a failed export says so
# ---------------------------------------------------------------------------


@pytest.fixture()
def _silent_message_box(monkeypatch):
    """Keep ``QMessageBox.exec`` from blocking the offscreen run.

    Everything else about the dialog — that it is built, and with what
    text — stays real, so the test still covers the path the user sees.
    """
    from PySide6.QtWidgets import QMessageBox

    shown: list[tuple[str, str]] = []

    def _exec(self) -> int:  # type: ignore[no-untyped-def]
        shown.append((self.text(), self.informativeText()))
        return 0

    monkeypatch.setattr(QMessageBox, "exec", _exec)
    return shown


@pytest.fixture()
def _export_destination(monkeypatch):
    """Answer the save dialog with a caller-chosen path."""
    import cortex.apps.desktop_shell.history_tab as history_mod

    def _use(path: object) -> None:
        monkeypatch.setattr(
            history_mod.QFileDialog,
            "getSaveFileName",
            staticmethod(lambda *a, **kw: (str(path), "")),
        )

    return _use


def test_a_failed_export_reaches_the_user(
    qapp, _no_native_chrome, _silent_message_box, _export_destination, tmp_path,
):
    """``_do_export`` caught every write failure and only logged at WARNING.

    There was no toast, dialog, status line or signal, so from the user's
    side a failed CSV/JSON export was indistinguishable from a successful
    one: they picked a destination, and nothing was there.
    """
    from cortex.apps.desktop_shell.history_tab import HistoryTab

    tab = HistoryTab()
    try:
        failures: list[str] = []
        tab.export_failed.connect(failures.append)

        target = tmp_path / "nonexistent-directory" / "session.json"
        _export_destination(target)

        tab._do_export("json", {"session_id": "s1"})

        assert not target.exists()
        assert failures, "a failed export must be reported, not just logged"
        assert _silent_message_box, "the user must see a dialog, not a log line"
        headline, detail = _silent_message_box[0]
        assert "could not write" in headline
        assert str(target) in detail
    finally:
        try:
            tab.deleteLater()
        except RuntimeError:
            pass


def test_a_write_that_leaves_nothing_behind_counts_as_a_failure(
    qapp, _no_native_chrome, _silent_message_box, _export_destination,
    tmp_path, monkeypatch,
):
    """Success is the file existing with content, not the call returning."""
    import pathlib

    from cortex.apps.desktop_shell.history_tab import HistoryTab

    tab = HistoryTab()
    try:
        failures: list[str] = []
        tab.export_failed.connect(failures.append)

        target = tmp_path / "session.json"
        _export_destination(target)
        # A write that reports success and produces nothing.
        monkeypatch.setattr(
            pathlib.Path, "write_text", lambda self, *a, **kw: 0
        )

        tab._do_export("json", {"session_id": "s1"})

        assert failures, "an empty output file is a failed export"
    finally:
        try:
            tab.deleteLater()
        except RuntimeError:
            pass
