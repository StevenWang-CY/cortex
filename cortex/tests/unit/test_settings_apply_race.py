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


# ---------------------------------------------------------------------------
# The high-water mark belongs to the socket, not the daemon process
# ---------------------------------------------------------------------------


def _sync(version: int, key: str):
    from cortex.services.api_gateway.websocket_server import WSMessage

    return WSMessage(
        type="SETTINGS_SYNC", payload={"settings_version": version, "k": key}
    )


def test_a_restarted_shell_is_not_locked_out_of_applying_settings(qapp):
    """A fresh socket starts from a clean ceiling.

    The producer of ``settings_version`` is ``SettingsDialog._settings_version``
    — an in-memory integer that starts at 0 in every new shell process and is
    never persisted. The daemon routinely outlives the shell (the documented
    WebSocket mode), so a process-global high-water mark meant that after the
    user had applied settings seven times, restarted the shell, and applied
    again, the daemon compared the new version 1 against its retained 7 and
    dropped it — and every apply after it, for the daemon's whole life. The
    dialog persisted the file and logged "Settings applied" regardless, so
    the settings the user was shown and the ones the daemon was running
    diverged with nothing visible to say so.
    """
    from cortex.services.api_gateway.websocket_server import (
        WebSocketClient,
        WebSocketServer,
    )

    server = WebSocketServer()
    received: list[dict] = []
    server.set_settings_callback(lambda payload: received.append(payload))

    first_shell = WebSocketClient(client_id="shell-1", websocket=object())
    second_shell = WebSocketClient(client_id="shell-2", websocket=object())

    async def _run() -> None:
        for version in range(1, 8):
            await server._handle_settings_sync(
                first_shell, _sync(version, f"old-{version}")
            )
        # The shell process restarts: same durable identity, counter at 0.
        server._clients.pop("shell-1", None)
        server._last_settings_version.pop("shell-1", None)
        await server._handle_settings_sync(second_shell, _sync(1, "new-1"))
        await server._handle_settings_sync(second_shell, _sync(2, "new-2"))

    asyncio.run(_run())

    assert [p["k"] for p in received][-2:] == ["new-1", "new-2"], (
        "a restarted shell must be able to apply settings again; got "
        f"{[p['k'] for p in received]}"
    )


def test_ordering_is_still_enforced_within_one_socket(qapp):
    """Per-socket keying must not weaken the guarantee F04 was built for."""
    from cortex.services.api_gateway.websocket_server import (
        WebSocketClient,
        WebSocketServer,
    )

    server = WebSocketServer()
    received: list[dict] = []
    server.set_settings_callback(lambda payload: received.append(payload))
    client = WebSocketClient(client_id="shell-1", websocket=object())

    async def _run() -> None:
        for version, key in ((2, "a"), (1, "b"), (2, "c"), (3, "d")):
            await server._handle_settings_sync(client, _sync(version, key))

    asyncio.run(_run())
    assert [p["k"] for p in received] == ["a", "d"]


def test_a_disconnect_releases_the_ceiling(qapp):
    """``_handle_client``'s teardown pops the entry, so nothing accumulates."""
    from cortex.services.api_gateway.websocket_server import (
        WebSocketClient,
        WebSocketServer,
    )

    server = WebSocketServer()
    server.set_settings_callback(lambda payload: None)
    client = WebSocketClient(client_id="shell-1", websocket=object())

    asyncio.run(server._handle_settings_sync(client, _sync(4, "a")))
    assert server._last_settings_version == {"shell-1": 4}

    asyncio.run(server.stop())
    assert server._last_settings_version == {}


@pytest.mark.parametrize("version", [0, -1, 2**31, 2**63, True, "3", 1.5])
def test_an_out_of_range_version_cannot_pin_the_ceiling(qapp, version):
    """One hostile frame must not set a ceiling no honest apply can clear.

    ``True`` is in the list because ``bool`` is an ``int`` subclass: without
    an explicit check it reads as version 1 and is applied. The non-integers
    are there because a present-but-unusable version used to fall straight
    through to the callback unchecked, which is the rewind F04 exists to
    prevent.
    """
    from cortex.services.api_gateway.websocket_server import (
        WebSocketClient,
        WebSocketServer,
    )

    server = WebSocketServer()
    received: list[dict] = []
    server.set_settings_callback(lambda payload: received.append(payload))
    client = WebSocketClient(client_id="shell-1", websocket=object())

    async def _run() -> None:
        await server._handle_settings_sync(client, _sync(version, "hostile"))
        await server._handle_settings_sync(client, _sync(1, "honest"))

    asyncio.run(_run())

    assert [p["k"] for p in received] == ["honest"], (
        f"version={version!r} must neither apply nor raise the ceiling"
    )


def test_a_dropped_apply_tells_the_sender(qapp):
    """The drop reaches the caller, not only the daemon log.

    The dialog has already written the file and logged success by the time
    the daemon decides, so a log-only drop is invisible to the user.
    """
    from cortex.services.api_gateway.websocket_server import (
        WebSocketClient,
        WebSocketServer,
    )

    server = WebSocketServer()
    server.set_settings_callback(lambda payload: None)
    client = WebSocketClient(client_id="shell-1", websocket=object())

    sent: list[tuple[str, dict]] = []

    async def _capture(target, message_type, payload, **kwargs):
        sent.append((message_type, payload))

    server._send_to = _capture  # type: ignore[assignment,method-assign]

    async def _run() -> None:
        await server._handle_settings_sync(client, _sync(3, "a"))
        await server._handle_settings_sync(client, _sync(2, "stale"))
        await server._handle_settings_sync(client, _sync(0, "bad"))

    asyncio.run(_run())

    codes = [payload["code"] for _type, payload in sent]
    assert codes == [
        "settings_sync_rejected:stale_version",
        "settings_sync_rejected:version_out_of_range",
    ], f"every dropped apply must be reported back; got {sent}"
    assert sent[0][1]["last_applied_version"] == 3
