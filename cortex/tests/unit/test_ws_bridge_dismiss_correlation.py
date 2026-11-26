"""WebSocket-mode desktop dismissals echo the trigger's correlation id.

The daemon drops a USER_ACTION whose cid differs from the one it stamped
on that intervention's INTERVENTION_TRIGGER, and a frame sent without a
cid is given a fresh one before the check, so it never matches. The
source-mode desktop shell (``CortexApp`` over ``WebSocketBridge``) sent
every overlay Dismiss and tray "Restore" without one, so the daemon
discarded them: the intervention stayed live on the browser and editor
surfaces and the dismissal never taught the trigger policy anything.
The in-process controller calls the daemon directly and is unaffected.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Coroutine
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from cortex.apps.desktop_shell import main as desktop_main  # noqa: E402
from cortex.apps.desktop_shell.message_router import DesktopMessageRouter  # noqa: E402


class _Sig:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def emit(self, payload: dict[str, Any]) -> None:
        self.payloads.append(payload)


@pytest.fixture()
def bridge(monkeypatch: pytest.MonkeyPatch) -> desktop_main.WebSocketBridge:
    instance = desktop_main.WebSocketBridge(host="127.0.0.1", port=9473)
    instance.intervention_triggered = _Sig()  # type: ignore[assignment]
    sent: list[dict[str, Any]] = []

    async def _capture(msg: str) -> None:
        sent.append(json.loads(msg))

    def _run_now(coro: Coroutine[Any, Any, None], _loop: object) -> None:
        runner = asyncio.new_event_loop()
        try:
            runner.run_until_complete(coro)
        finally:
            runner.close()

    instance._send = _capture  # type: ignore[method-assign]
    instance._loop = object()  # type: ignore[assignment]
    instance._ws = object()
    monkeypatch.setattr(desktop_main.asyncio, "run_coroutine_threadsafe", _run_now)
    instance.sent = sent  # type: ignore[attr-defined]
    return instance


def _trigger(intervention_id: str, cid: str, sequence: int) -> str:
    return json.dumps({
        "type": "INTERVENTION_TRIGGER",
        "payload": {"intervention_id": intervention_id},
        "sequence": sequence,
        "correlation_id": cid,
    })


def test_dismiss_carries_the_trigger_cid(bridge: desktop_main.WebSocketBridge) -> None:
    bridge._handle_message(_trigger("iv-1", "iv_iv-1_4", 4))
    bridge.send_user_action("dismissed", "iv-1")

    frame = bridge.sent[-1]  # type: ignore[attr-defined]
    assert frame["type"] == "USER_ACTION"
    assert frame["correlation_id"] == "iv_iv-1_4"


def test_each_intervention_keeps_its_own_cid(bridge: desktop_main.WebSocketBridge) -> None:
    bridge._handle_message(_trigger("iv-1", "iv_iv-1_4", 4))
    bridge._handle_message(_trigger("iv-2", "iv_iv-2_6", 6))
    bridge.send_user_action("dismissed", "iv-1")
    bridge.send_user_action("dismissed", "iv-2")

    cids = [frame["correlation_id"] for frame in bridge.sent]  # type: ignore[attr-defined]
    assert cids == ["iv_iv-1_4", "iv_iv-2_6"]


def test_a_stale_trigger_does_not_overwrite_the_cid(
    bridge: desktop_main.WebSocketBridge,
) -> None:
    bridge._handle_message(_trigger("iv-1", "iv_iv-1_8", 8))
    # A reordered older frame is dropped by the router and must not win.
    bridge._handle_message(_trigger("iv-1", "iv_iv-1_3", 3))
    bridge.send_user_action("dismissed", "iv-1")

    assert bridge.sent[-1]["correlation_id"] == "iv_iv-1_8"  # type: ignore[attr-defined]


def test_actions_without_an_intervention_send_no_cid(
    bridge: desktop_main.WebSocketBridge,
) -> None:
    bridge._handle_message(_trigger("iv-1", "iv_iv-1_4", 4))
    bridge.send_user_action("set_goal:write", "")

    assert "correlation_id" not in bridge.sent[-1]  # type: ignore[attr-defined]


def test_router_exposes_the_envelope_cid_only_during_dispatch() -> None:
    seen: list[str | None] = []
    router = DesktopMessageRouter({})
    router = DesktopMessageRouter(
        {"INTERVENTION_TRIGGER": lambda _payload: seen.append(router.current_correlation_id)},
    )

    router.dispatch_json(_trigger("iv-1", "iv_iv-1_4", 4))
    router.dispatch("INTERVENTION_TRIGGER", {}, sequence=5)

    assert seen == ["iv_iv-1_4", None]
    assert router.current_correlation_id is None
