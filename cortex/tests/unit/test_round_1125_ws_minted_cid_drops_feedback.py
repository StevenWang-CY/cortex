"""A cid-less USER_ACTION/USER_RATING must reach the daemon over the wire.

``_handle_user_action`` honours a frame without a correlation id as a
legacy client (``test_ws_user_action_cid`` pins that by calling the handler
directly). On the real receive path ``_process_message`` mints a fresh cid
for every frame that lacks one *before* dispatch, so the handler can no
longer tell "client sent no cid" from "client echoed a stale cid" and drops
the frame. The browser extension sends ``USER_RATING`` and several
``USER_ACTION`` frames without a correlation id, so a thumbs rating on any
delivered intervention was discarded.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from cortex.libs.schemas.intervention import InterventionPlan, UIPlan
from cortex.services.api_gateway.websocket_server import (
    WebSocketClient,
    WebSocketServer,
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


class _Sock:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, raw: str) -> None:
        self.sent.append(raw)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        return None


def _authenticated_chrome(server: WebSocketServer) -> WebSocketClient:
    client = WebSocketClient(client_id="c1", websocket=_Sock(), client_type="chrome")
    client.authenticated = True
    server._clients["c1"] = client
    return client


def _frame(message_type: str, payload: dict[str, Any]) -> str:
    # The shape the browser's ``send()`` produces for USER_RATING: no
    # ``correlation_id`` key at all (``withWireMetadata`` never adds one).
    return json.dumps(
        {
            "type": message_type,
            "payload": payload,
            "timestamp": 1_700_000_000.0,
            "sequence": 7,
        }
    )


@pytest.mark.asyncio
async def test_cidless_user_rating_reaches_the_daemon_after_a_trigger() -> None:
    server = WebSocketServer()
    client = _authenticated_chrome(server)
    captured: list[dict[str, Any]] = []
    server.set_user_action_callback(lambda payload: captured.append(payload))

    server._make_intervention_trigger(_plan("iv-rate"))
    await server._process_message(
        client,
        _frame("USER_RATING", {"intervention_id": "iv-rate", "rating": "thumbs_up"}),
    )

    assert [p.get("rating") for p in captured] == ["thumbs_up"]


@pytest.mark.asyncio
async def test_cidless_user_action_reaches_the_daemon_after_a_trigger() -> None:
    server = WebSocketServer()
    client = _authenticated_chrome(server)
    captured: list[dict[str, Any]] = []
    server.set_user_action_callback(lambda payload: captured.append(payload))

    server._make_intervention_trigger(_plan("iv-dismiss"))
    await server._process_message(
        client,
        _frame("USER_ACTION", {"intervention_id": "iv-dismiss", "action": "dismissed"}),
    )

    assert [p.get("action") for p in captured] == ["dismissed"]


@pytest.mark.asyncio
async def test_stale_echoed_cid_is_still_dropped_on_the_wire_path() -> None:
    server = WebSocketServer()
    client = _authenticated_chrome(server)
    captured: list[dict[str, Any]] = []
    server.set_user_action_callback(lambda payload: captured.append(payload))

    first = server._make_intervention_trigger(_plan("iv-stale"))
    server._make_intervention_trigger(_plan("iv-stale"))
    frame = json.loads(_frame("USER_ACTION", {"intervention_id": "iv-stale", "action": "dismissed"}))
    frame["correlation_id"] = first.correlation_id
    await server._process_message(client, json.dumps(frame))

    assert captured == []
