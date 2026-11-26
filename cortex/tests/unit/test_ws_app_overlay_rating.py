"""WebSocket-mode overlay ratings reach the daemon.

The overlay emits ``rating_invoked(intervention_id, rating, text)`` for
👍/👎 and the 👎 comment. The in-process controller forwards it as a
USER_RATING; the source-mode shell (``CortexApp``) never connected the
signal, so every rating made on the desktop overlay was discarded. The
frame must also echo the trigger's cid, or the daemon drops it.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Coroutine
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    pytest.skip("PySide6 not available", allow_module_level=True)

from cortex.apps.desktop_shell import main as desktop_main  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    return app  # type: ignore[return-value]


class _Bridge:
    def __init__(self) -> None:
        self.ratings: list[tuple[str, str, str]] = []
        self.actions: list[tuple[str, str]] = []

    def send_user_rating(self, intervention_id: str, rating: str, text: str = "") -> None:
        self.ratings.append((intervention_id, rating, text))

    def send_user_action(self, action: str, intervention_id: str) -> None:
        self.actions.append((action, intervention_id))


def test_overlay_rating_is_forwarded(qapp: QApplication) -> None:
    from cortex.apps.desktop_shell.overlay import OverlayWindow

    app = desktop_main.CortexApp()
    app._overlay = OverlayWindow()
    app._bridge = _Bridge()  # type: ignore[assignment]
    app._wire_overlay()
    try:
        app._overlay.rating_invoked.emit("iv-1", "thumbs_down", "too early")
        app._overlay.dismissed.emit("iv-1")
        assert app._bridge.ratings == [("iv-1", "thumbs_down", "too early")]  # type: ignore[union-attr]
        assert app._bridge.actions == [("dismissed", "iv-1")]  # type: ignore[union-attr]
    finally:
        app._overlay.close()
        app._overlay.deleteLater()


def test_bridge_rating_frame_carries_the_trigger_cid(monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = desktop_main.WebSocketBridge(host="127.0.0.1", port=9473)

    class _Sig:
        def emit(self, *_args: Any) -> None:
            pass

    bridge.intervention_triggered = _Sig()  # type: ignore[assignment]
    sent: list[dict[str, Any]] = []

    async def _capture(msg: str) -> None:
        sent.append(json.loads(msg))

    def _run_now(coro: Coroutine[Any, Any, None], _loop: object) -> None:
        runner = asyncio.new_event_loop()
        try:
            runner.run_until_complete(coro)
        finally:
            runner.close()

    bridge._send = _capture  # type: ignore[method-assign]
    bridge._loop = object()  # type: ignore[assignment]
    bridge._ws = object()
    monkeypatch.setattr(desktop_main.asyncio, "run_coroutine_threadsafe", _run_now)

    bridge._handle_message(json.dumps({
        "type": "INTERVENTION_TRIGGER",
        "payload": {"intervention_id": "iv-1"},
        "sequence": 3,
        "correlation_id": "iv_iv-1_3",
    }))
    bridge.send_user_rating("iv-1", "thumbs_up")
    bridge.send_user_rating("iv-1", "thumbs_down", "x" * 300)

    assert [frame["type"] for frame in sent] == ["USER_RATING", "USER_RATING"]
    assert sent[0]["payload"] == {"intervention_id": "iv-1", "rating": "thumbs_up"}
    assert sent[0]["correlation_id"] == "iv_iv-1_3"
    assert sent[1]["payload"]["context"] == "x" * 200
