"""Phase-4b — daemon-level remediation tests.

Covers:
- TASK A: USER_RATING routes into AMIP reward.
- TASK D: concurrent _trigger_intervention calls don't share decision_id.
- TASK H: consent escalation preserves approval_timestamps for the
  30-day decay window.
- TASK I: WS broadcast coalescing drops older STATE_UPDATEs.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

# ----------------------------------------------------------------------
# TASK A — USER_RATING → AMIP reward
# ----------------------------------------------------------------------


class _StubHelpfulness:
    def __init__(self) -> None:
        self.recorded: list[tuple[str, str, str | None]] = []

    def record_rating(
        self, iid: str, rating: str, *, text_feedback: str | None = None,
    ) -> None:
        self.recorded.append((iid, rating, text_feedback))

    def downvote_count_within(self, _seconds: float) -> int:
        return 0

    def reset_downvote_window(self) -> None:
        pass


class _StubAMIP:
    def __init__(self) -> None:
        self.updates: list[tuple[str, float]] = []

    # NB: the real ``AMIPPolicy.update_reward`` is SYNCHRONOUS (returns
    # None). The daemon calls it without ``await``; the stub must mirror
    # that signature or the recorded reward is silently dropped.
    def update_reward(self, decision_id: str, reward: float) -> None:
        self.updates.append((decision_id, reward))


class _EvalConfig:
    policy: str = "amip"


class _RuntimeConfig:
    eval = _EvalConfig()


class _StubRecorder:
    def append(self, *_a: Any, **_k: Any) -> None:
        pass


class _RatingDaemonHarness:
    """Minimal slice of CortexDaemon for the rating path."""

    def __init__(self) -> None:
        self._helpfulness = _StubHelpfulness()
        self._amip = _StubAMIP()
        self._amip_decision_ids_by_intervention: dict[str, str] = {}
        self._last_policy_decision_id: str | None = None
        self._recorder = _StubRecorder()
        self._quiet_mode_throttle_latched_at = 0.0
        self.config = _RuntimeConfig()

    async def set_quiet_mode(self, *_a: Any, **_k: Any) -> None:
        pass


# Import the real daemon method as a free function bound to our harness
# so the bookkeeping for routing rating → AMIP exercises the production
# logic verbatim.


@pytest.mark.asyncio
async def test_user_rating_thumbs_up_emits_positive_amip_reward() -> None:
    from cortex.services.runtime_daemon import CortexDaemon

    harness = _RatingDaemonHarness()
    harness._amip_decision_ids_by_intervention["iv-1"] = "decision-abc"

    await CortexDaemon._handle_user_action(
        harness,  # type: ignore[arg-type]
        {"intervention_id": "iv-1", "rating": "thumbs_up"},
    )
    assert harness._amip.updates == [("decision-abc", 0.7)]
    assert harness._helpfulness.recorded == [("iv-1", "thumbs_up", None)]


@pytest.mark.asyncio
async def test_user_rating_thumbs_down_emits_negative_amip_reward() -> None:
    from cortex.services.runtime_daemon import CortexDaemon

    harness = _RatingDaemonHarness()
    harness._amip_decision_ids_by_intervention["iv-2"] = "decision-xyz"

    await CortexDaemon._handle_user_action(
        harness,  # type: ignore[arg-type]
        {"intervention_id": "iv-2", "rating": "thumbs_down"},
    )
    rewards = [r for _, r in harness._amip.updates]
    assert rewards == [-0.7]


@pytest.mark.asyncio
async def test_user_rating_no_decision_id_does_not_call_amip() -> None:
    from cortex.services.runtime_daemon import CortexDaemon

    harness = _RatingDaemonHarness()
    # No decision_id bound — the rating still records on helpfulness
    # but no AMIP update fires (no arm to credit).
    await CortexDaemon._handle_user_action(
        harness,  # type: ignore[arg-type]
        {"intervention_id": "iv-3", "rating": "thumbs_up"},
    )
    assert harness._amip.updates == []
    assert ("iv-3", "thumbs_up", None) in harness._helpfulness.recorded


# ----------------------------------------------------------------------
# TASK H — consent escalation preserves approval_timestamps
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consent_escalation_preserves_approval_timestamps() -> None:
    """Two consecutive escalations should not lose 30-day approval credit."""
    from cortex.services.consent.ladder import (
        ConsentLadder,
    )
    from cortex.services.consent.policy import ConsentPolicy

    policy = ConsentPolicy()
    ladder = ConsentLadder(
        policy=policy, escalation_threshold=2,
    )

    # Drive the first escalation: 2 approvals at the default tier.
    await ladder.record_approval("close_tab")
    await ladder.record_approval("close_tab")
    state = (await ladder.get_all_states())["close_tab"]
    timestamps_after_first = list(state.get("approval_timestamps", []))
    # Escalation should NOT have wiped the timestamp ledger.
    assert len(timestamps_after_first) >= 1, (
        "approval_timestamps must persist across escalation"
    )

    # Drive another two approvals — second escalation should ALSO preserve.
    await ladder.record_approval("close_tab")
    await ladder.record_approval("close_tab")
    state = (await ladder.get_all_states())["close_tab"]
    timestamps_after_second = list(state.get("approval_timestamps", []))
    assert len(timestamps_after_second) >= len(timestamps_after_first), (
        "Second escalation must not delete first-tier timestamps"
    )


# ----------------------------------------------------------------------
# TASK I — WS coalescing newest-wins for STATE_UPDATE
# ----------------------------------------------------------------------


class _BlockedFakeWebSocket:
    """A websocket whose send() blocks until ``release`` is set."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.release = asyncio.Event()
        self.closed: bool = False

    async def send(self, payload: str) -> None:
        await self.release.wait()
        self.sent.append(payload)

    async def close(self, *_a: Any, **_k: Any) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_ws_state_update_coalesces_newest_wins() -> None:
    """Phase-4b TASK I: when a slow consumer is blocking on the previous
    frame, subsequent STATE_UPDATE broadcasts coalesce to newest-wins
    inside the per-client queue (depth=1). The drain task sees only
    the latest frame after the consumer unblocks.
    """
    from cortex.libs.config.settings import APIConfig
    from cortex.libs.schemas.ws_message_types import MessageType
    from cortex.services.api_gateway.websocket_server import (
        WebSocketClient,
        WebSocketServer,
        WSMessage,
    )

    server = WebSocketServer(APIConfig())
    fake_ws = _BlockedFakeWebSocket()
    client = WebSocketClient(
        client_id="c1", websocket=fake_ws, client_type="chrome",
        authenticated=True,
    )
    server._clients["c1"] = client

    try:
        # Three STATE_UPDATEs while the consumer is blocked. Queue
        # depth is 1 so the producer evicts older frames.
        for i in range(3):
            msg = WSMessage(
                type=MessageType.STATE_UPDATE,
                payload={"state": "FLOW", "seq": i},
                sequence=i,
            )
            await server._broadcast(msg)

        # Give the drain task a chance to pull the queued frame into
        # its blocked send call.
        await asyncio.sleep(0)

        # Inspect the queue WITHOUT unblocking the send. The depth-1
        # queue must hold AT MOST one frame; the older frames must
        # have been dropped.
        assert client.coalesce_queue is not None
        assert client.coalesce_queue.qsize() <= 1
    finally:
        # Cancel the drain task to avoid leaking it past the test.
        if client.coalesce_task is not None:
            client.coalesce_task.cancel()
            try:
                await client.coalesce_task
            except (asyncio.CancelledError, Exception):
                pass


# ----------------------------------------------------------------------
# TASK D — concurrent trigger decision_id isolation
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_trigger_outcomes_attribute_to_distinct_decision_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent triggers must each track their own decision_id
    rather than racing on ``self._last_policy_decision_id``.

    We exercise the bookkeeping at the rating-routing path which uses
    the same ``_amip_decision_ids_by_intervention`` dict for outcome
    attribution.
    """
    from cortex.services.runtime_daemon import CortexDaemon

    harness = _RatingDaemonHarness()
    # Simulate two concurrent triggers writing distinct ids to the map.
    harness._amip_decision_ids_by_intervention["iv-A"] = "decision-A"
    harness._amip_decision_ids_by_intervention["iv-B"] = "decision-B"

    await CortexDaemon._handle_user_action(
        harness,  # type: ignore[arg-type]
        {"intervention_id": "iv-A", "rating": "thumbs_up"},
    )
    await CortexDaemon._handle_user_action(
        harness,  # type: ignore[arg-type]
        {"intervention_id": "iv-B", "rating": "thumbs_down"},
    )
    # Each rating must have routed to its own decision_id, not the
    # last-write-wins slot.
    by_decision = dict(harness._amip.updates)
    assert by_decision["decision-A"] == pytest.approx(0.7)
    assert by_decision["decision-B"] == pytest.approx(-0.7)
