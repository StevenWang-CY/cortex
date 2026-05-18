"""F16-srv: daemon ignores USER_ACTION ACKs whose correlation_id does
not match the cid stamped on the most-recent INTERVENTION_TRIGGER.

Regression guard for the atomic-swap protocol introduced in F16 on the
browser-extension side.
"""

from __future__ import annotations

import pytest

from cortex.libs.schemas.intervention import InterventionPlan, UIPlan
from cortex.services.api_gateway.websocket_server import (
    WebSocketClient,
    WebSocketServer,
    WSMessage,
)


def _plan(intervention_id: str) -> InterventionPlan:
    return InterventionPlan(
        intervention_id=intervention_id,
        level="overlay_only",
        situation_summary="Test plan",
        headline="Test",
        primary_focus="Test",
        micro_steps=["a"],
        ui_plan=UIPlan(
            dim_background=False,
            show_overlay=True,
            fold_unrelated_code=False,
            intervention_type="overlay_only",
        ),
        tone="supportive",
    )


def _client(server: WebSocketServer) -> WebSocketClient:
    class _Sock:
        async def send(self, _raw: str) -> None:  # pragma: no cover
            return

    c = WebSocketClient(client_id="c1", websocket=_Sock(), client_type="chrome")
    server._clients["c1"] = c
    return c


@pytest.mark.asyncio
async def test_stale_cid_user_action_is_dropped() -> None:
    server = WebSocketServer()
    client = _client(server)

    captured: list[dict[str, object]] = []

    def cb(payload: dict[str, object]) -> None:
        captured.append(payload)

    server.set_user_action_callback(cb)

    # Two emissions of the same intervention_id; latest cid wins.
    msg1 = server._make_intervention_trigger(_plan("iv1"))
    msg2 = server._make_intervention_trigger(_plan("iv1"))
    assert msg1.correlation_id != msg2.correlation_id

    # ACK with the stale cid → dropped.
    stale_ack = WSMessage(
        type="USER_ACTION",
        payload={"action": "dismissed", "intervention_id": "iv1"},
        correlation_id=msg1.correlation_id,
    )
    await server._handle_user_action(client, stale_ack)
    assert captured == []

    # ACK with the fresh cid → delivered.
    fresh_ack = WSMessage(
        type="USER_ACTION",
        payload={"action": "engaged", "intervention_id": "iv1"},
        correlation_id=msg2.correlation_id,
    )
    await server._handle_user_action(client, fresh_ack)
    assert len(captured) == 1
    assert captured[0]["action"] == "engaged"


@pytest.mark.asyncio
async def test_user_action_without_cid_honoured_as_legacy() -> None:
    """A USER_ACTION lacking correlation_id is treated as a pre-F16
    client and delivered without enforcement so the rollout is staged."""

    server = WebSocketServer()
    client = _client(server)

    captured: list[dict[str, object]] = []
    server.set_user_action_callback(lambda p: captured.append(p))

    server._make_intervention_trigger(_plan("iv1"))
    legacy_ack = WSMessage(
        type="USER_ACTION",
        payload={"action": "dismissed", "intervention_id": "iv1"},
        correlation_id=None,
    )
    await server._handle_user_action(client, legacy_ack)
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_intervention_trigger_stamps_correlation_id() -> None:
    server = WebSocketServer()
    msg = server._make_intervention_trigger(_plan("iv-cid-test"))
    assert msg.correlation_id is not None
    assert "iv-cid-test" in msg.correlation_id
