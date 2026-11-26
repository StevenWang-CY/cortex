"""The desktop WebSocket bridge never routes the loopback daemon through a proxy.

``websockets`` 15+ defaults to ``proxy=True``: it asks
``urllib.request.getproxies()`` (environment variables, or on macOS the
system proxy settings) and uses the proxy unless ``proxy_bypass`` matches.
Neither the macOS default bypass list ("*.local, 169.254/16") nor an
environment proxy without NO_PROXY covers ``127.0.0.1``, so on a Mac
with a configured proxy the source-mode shell sent its AUTH token to the
proxy and never reached the daemon. The bridge talks only to loopback
and must connect directly. (05c748b made the same fix for the privacy
controller's ``urlopen`` calls.)
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from cortex.apps.desktop_shell import main as desktop_main  # noqa: E402


def test_connect_disables_proxy_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    import websockets

    bridge = desktop_main.WebSocketBridge(host="127.0.0.1", port=9473)

    class _Sig:
        def emit(self, *_args: Any) -> None:
            pass

    bridge.connection_changed = _Sig()  # type: ignore[assignment]
    calls: list[tuple[str, dict[str, Any]]] = []

    def _fake_connect(uri: str, **kwargs: Any) -> Any:
        calls.append((uri, kwargs))
        bridge._running = False
        raise OSError("refused")

    monkeypatch.setattr(websockets, "connect", _fake_connect)
    bridge._running = True
    asyncio.run(bridge._connect_loop())

    assert calls, "the bridge never attempted a connection"
    uri, kwargs = calls[0]
    assert uri == "ws://127.0.0.1:9473"
    assert "proxy" in kwargs and kwargs["proxy"] is None
