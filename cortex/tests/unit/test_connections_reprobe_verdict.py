"""The Connections card's verdict must agree with the checks it re-ran.

The panel re-probes the bridge, the extension and the daemon every time it
is shown, but ``reprobe`` only refreshed the three checklist rows and kept
the card's note and next steps. Clicking Verify before relaunching Chrome
(the usual first attempt -- a native host is only picked up after Cmd+Q)
left "Not connected yet" with "Load the copied extension folder..." on the
card; after the relaunch, reopening the panel showed three green checks
under that same red "Not connected yet". The reverse also held: a card
verified earlier kept "Everything Cortex can check here is ready" after the
session ended and the daemon row turned red.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from cortex.apps.desktop_shell import connections as conn


@pytest.fixture()
def panel(monkeypatch: pytest.MonkeyPatch) -> Iterator[conn.ConnectionsPanel]:
    _app = QApplication.instance() or QApplication([])
    widget = conn.ConnectionsPanel()
    # One installed, non-translocated browser; nothing real is probed.
    widget._browser_cards = [("Chrome", "chrome://extensions", True)]
    widget._translocated = False
    try:
        yield widget
    finally:
        widget.deleteLater()


def _set_world(
    monkeypatch: pytest.MonkeyPatch,
    *,
    bridge: bool,
    extension: bool,
    daemon: bool,
) -> None:
    from cortex.scripts import install_native_host

    monkeypatch.setattr(conn, "_probe_bridge", lambda _app: (bridge, extension, ""))
    monkeypatch.setattr(
        conn.ConnectionsPanel, "_daemon_reachable", lambda self, **_kw: daemon
    )
    monkeypatch.setattr(
        install_native_host,
        "verify_browser_installation",
        lambda _app: SimpleNamespace(
            ok=bridge,
            extension_ids=("abc",) if extension else (),
            error="" if bridge else "manifest missing",
        ),
    )


def test_reprobe_replaces_a_stale_not_connected_verdict(
    panel: conn.ConnectionsPanel, monkeypatch: pytest.MonkeyPatch,
) -> None:
    verified: list[str] = []
    panel.connection_verified.connect(verified.append)

    _set_world(monkeypatch, bridge=True, extension=False, daemon=True)
    panel._verify_browser_connection("Chrome")
    assert panel.status_for("Chrome")["tone"] == "danger"

    # The user loads the extension and relaunches Chrome, then reopens.
    _set_world(monkeypatch, bridge=True, extension=True, daemon=True)
    panel.reprobe()

    status = panel.status_for("Chrome")
    assert status["rows"] == {"bridge": True, "extension": True, "daemon": True}
    assert status["tone"] == "success"
    assert "Not connected" not in status["note"]
    assert status["steps"] == []
    assert verified == ["Chrome"]


def test_reprobe_withdraws_a_stale_ready_verdict(
    panel: conn.ConnectionsPanel, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_world(monkeypatch, bridge=True, extension=True, daemon=True)
    panel._verify_browser_connection("Chrome")
    assert panel.status_for("Chrome")["tone"] == "success"

    # The session ended; the daemon is gone.
    _set_world(monkeypatch, bridge=True, extension=True, daemon=False)
    panel.reprobe()

    status = panel.status_for("Chrome")
    assert status["rows"]["daemon"] is False
    assert status["tone"] == "danger"
    assert "ready" not in status["note"]
    assert any("Start a session" in step for step in status["steps"])


def test_reprobe_keeps_setup_guidance_mid_flow(
    panel: conn.ConnectionsPanel, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # After Connect: bridge registered, the four finish steps on screen.
    finish = conn._finish_steps("Chrome")
    panel._set_status(
        "Chrome", rows={"bridge": True}, note="finish these steps", tone="success",
        steps=finish,
    )

    _set_world(monkeypatch, bridge=True, extension=False, daemon=True)
    panel.reprobe()

    status = panel.status_for("Chrome")
    assert status["steps"] == finish
    assert status["note"] == "finish these steps"
