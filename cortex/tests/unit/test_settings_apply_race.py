"""F04 — Settings Apply double-click reentrancy.

Run with: ``QT_QPA_PLATFORM=offscreen pytest cortex/tests/unit/test_settings_apply_race.py``

The Apply button now coalesces double-clicks via a QMutex + a button
disable while the apply is in flight. Every apply stamps the payload
with a monotonic ``settings_version``; the daemon-side WS handler drops
any sync whose version is older than the last applied one.
"""

from __future__ import annotations

import asyncio
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
def dialog(qapp, monkeypatch):
    from cortex.apps.desktop_shell import mac_native, settings as settings_mod

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


def test_single_apply_roundtrips(dialog):
    captured: list[dict] = []
    dialog.settings_changed.connect(lambda s: captured.append(s))

    dialog._apply_settings()

    assert len(captured) == 1
    assert captured[0]["settings_version"] == 1
    assert dialog._apply_btn.isEnabled()


def test_double_click_coalesces(dialog):
    """Two synchronous clicks must emit only one settings_changed (the
    second click finds the mutex held and bails). Versions monotonically
    advance — second emitted apply (after first releases) would be v2."""
    captured: list[dict] = []
    dialog.settings_changed.connect(lambda s: captured.append(s))

    # Hold the mutex from outside so the first click sees it taken
    # (simulates an apply still in flight).
    assert dialog._apply_mutex.tryLock()
    try:
        dialog._apply_settings()
        dialog._apply_settings()
    finally:
        dialog._apply_mutex.unlock()

    assert captured == [], "no emission while mutex held"

    # Now a normal apply succeeds.
    dialog._apply_settings()
    assert len(captured) == 1
    assert captured[0]["settings_version"] == 1


def test_stale_settings_version_discarded(qapp):
    """Daemon-side: WS handler drops any payload whose settings_version is
    not strictly greater than the last accepted one."""
    from cortex.services.api_gateway.websocket_server import (
        WebSocketClient,
        WebSocketServer,
        WSMessage,
    )

    server = WebSocketServer()
    received: list[dict] = []
    server.set_settings_callback(lambda payload: received.append(payload))

    client = WebSocketClient(client_id="c1", websocket=object())

    async def _run() -> None:
        # First (version=2) accepted.
        await server._handle_settings_sync(
            client,
            WSMessage(type="SETTINGS_SYNC", payload={"settings_version": 2, "k": "a"}),
        )
        # Stale (version=1) dropped.
        await server._handle_settings_sync(
            client,
            WSMessage(type="SETTINGS_SYNC", payload={"settings_version": 1, "k": "b"}),
        )
        # Same version (==2) also dropped — must be strictly greater.
        await server._handle_settings_sync(
            client,
            WSMessage(type="SETTINGS_SYNC", payload={"settings_version": 2, "k": "c"}),
        )
        # Newer (version=3) accepted.
        await server._handle_settings_sync(
            client,
            WSMessage(type="SETTINGS_SYNC", payload={"settings_version": 3, "k": "d"}),
        )

    asyncio.run(_run())

    assert [p["k"] for p in received] == ["a", "d"], (
        f"stale versions must be dropped; got {[p['k'] for p in received]}"
    )


def test_apply_button_reenables_after_callback(dialog):
    """The Apply button must be re-enabled in the ``finally`` clause even
    if the settings_changed slot raises."""

    def _raising_slot(_payload: dict) -> None:
        raise RuntimeError("simulated downstream failure")

    dialog.settings_changed.connect(_raising_slot)

    # The signal emission propagates exceptions in Qt 6 — wrap to assert.
    try:
        dialog._apply_settings()
    except RuntimeError:
        pass

    assert dialog._apply_btn.isEnabled(), (
        "Apply button must re-enable even when downstream slot raises"
    )
    # And the mutex must have been released.
    assert dialog._apply_mutex.tryLock()
    dialog._apply_mutex.unlock()
