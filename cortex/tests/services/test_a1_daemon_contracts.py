"""A1 remediation: PIPE-REPORT wiring, CONTRACT-2 WS cost parity, CONTRACT-3.

Daemon/WS-level coverage for three Phase-4 items whose owner agent died
mid-run; the lead implemented the fixes and these fail before / pass after.
"""

from __future__ import annotations

import pytest

from cortex.libs.schemas.session_history import TrendsResponse
from cortex.libs.schemas.ws_message import WSMessage
from cortex.services.api_gateway.websocket_server import (
    WebSocketClient,
    WebSocketServer,
)
from cortex.services.runtime_daemon import CortexDaemon

# ─── PIPE-REPORT: _handle_activity_sync wires the SessionReport producers ──
# Before the fix the four generator producers had ZERO call sites, so every
# SessionReport persisted interventions_*=0 and empty activity/distraction
# lists, and the longitudinal task-pattern rollup was permanently empty.


@pytest.mark.asyncio
async def test_activity_sync_populates_session_report() -> None:
    daemon = CortexDaemon()
    activities = [
        {"url": "https://www.reddit.com/r/python", "title": "Reddit", "duration_spent_s": 120},
        {"url": "https://stackoverflow.com/q/1", "title": "SO answer", "duration_spent_s": 60},
        {"url": "https://www.youtube.com/watch?v=x", "title": "YT video", "duration_spent_s": 200},
    ]
    await daemon._handle_activity_sync({"activities": activities})

    report = daemon._session_report.finish()
    # All three activities recorded.
    assert len(report.top_activities) == 3
    # reddit (social) + youtube (video_platform) are distractions; the
    # stackoverflow tab is work and must NOT be flagged as a distraction.
    domains = set(report.top_distraction_domains)
    assert "reddit.com" in domains
    assert "youtube.com" in domains
    assert "stackoverflow.com" not in domains


@pytest.mark.asyncio
async def test_activity_sync_ignores_non_list_and_bad_items() -> None:
    daemon = CortexDaemon()
    # Non-list payload is a no-op (must not raise).
    await daemon._handle_activity_sync({"activities": "nope"})
    # Mixed list with a non-dict item: the dict is still processed.
    await daemon._handle_activity_sync(
        {"activities": [42, {"url": "https://twitter.com/x", "title": "T", "duration_spent_s": 5}]}
    )
    report = daemon._session_report.finish()
    assert "twitter.com" in set(report.top_distraction_domains)


# ─── CONTRACT-2: WS COST_RESPONSE carries the same keys as HTTP /api/cost ──


@pytest.mark.asyncio
async def test_ws_cost_response_populates_tokens_and_model() -> None:
    daemon = CortexDaemon()

    class _Tracker:
        prompt_tokens_today = 100
        completion_tokens_today = 50

        def today_total_usd(self) -> float:
            return 1.23

        def check_budget(self) -> str:
            return "OK"

    class _Client:
        _cost_tracker = _Tracker()
        model = "claude-test-model"

    daemon._llm_client = _Client()  # type: ignore[assignment]
    resp = await daemon.get_cost_response()
    # Before the fix the WS path left these three null; now it probes them
    # via the same helper the HTTP route uses.
    assert resp.prompt_tokens == 100
    assert resp.completion_tokens == 50
    assert resp.model == "claude-test-model"
    assert resp.cost_today == 1.23


# ─── CONTRACT-3: no-handler TRENDS frame sends chronotype={} (not None) ────


@pytest.mark.asyncio
async def test_request_trends_no_handler_sends_validatable_frame() -> None:
    server = WebSocketServer()
    # No trends callback wired -> the degradation path runs.
    assert server._trends_callback is None

    sent: list[tuple[str, dict]] = []

    async def _capture(message_type: str, payload: dict, **_: object) -> int:
        sent.append((message_type, payload))
        return 1

    server.send_message = _capture  # type: ignore[assignment, method-assign]

    client = WebSocketClient(client_id="c1", websocket=None, client_type="chrome")
    msg = WSMessage(type="REQUEST_TRENDS")
    await server._handle_request_trends(client, msg)

    assert sent, "expected a TRENDS frame to be sent"
    _, payload = sent[0]
    # chronotype MUST be a dict (empty object), never null...
    assert payload["chronotype"] == {}
    assert payload["error"] == "handler_not_registered"
    # ...and the frame must round-trip through the non-null schema.
    TrendsResponse.model_validate(payload)
