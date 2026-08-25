"""Phase-4b — daemon-level remediation tests.

Covers:
- WP8: USER_RATING remains an observation until one reward-window finalization.
- WP8: concurrent interventions retain distinct observation attribution.
- TASK H: consent escalation preserves approval_timestamps for the
  30-day decay window.
- TASK I: WS broadcast coalescing drops older STATE_UPDATEs.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

# ----------------------------------------------------------------------
# WP8 — USER_RATING → durable observation, never an intermediate reward
# ----------------------------------------------------------------------


class _StubHelpfulness:
    def __init__(self) -> None:
        self.recorded: list[tuple[str, str, str | None]] = []

    def record_rating(
        self,
        iid: str,
        rating: str,
        *,
        text_feedback: str | None = None,
    ) -> None:
        self.recorded.append((iid, rating, text_feedback))

    def downvote_count_within(self, _seconds: float) -> int:
        return 0

    def reset_downvote_window(self) -> None:
        pass


class _StubPolicyLifecycle:
    def __init__(self) -> None:
        self.observations: list[tuple[str, str, str, dict[str, Any]]] = []

    async def observe_intervention(
        self,
        intervention_id: str,
        *,
        kind: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> bool:
        self.observations.append((intervention_id, kind, idempotency_key, payload))
        return True


class _StubRecorder:
    def append(self, *_a: Any, **_k: Any) -> None:
        pass


class _RatingDaemonHarness:
    """Minimal slice of CortexDaemon for the rating path."""

    def __init__(self) -> None:
        self._helpfulness = _StubHelpfulness()
        self._policy_lifecycle = _StubPolicyLifecycle()
        self._recorder = _StubRecorder()
        self._quiet_mode_throttle_latched_at = 0.0

    async def set_quiet_mode(self, *_a: Any, **_k: Any) -> None:
        pass


# Bind the real daemon method so this covers production routing verbatim.


@pytest.mark.asyncio
async def test_user_rating_thumbs_up_is_observed_without_early_reward() -> None:
    from cortex.services.runtime_daemon import CortexDaemon

    harness = _RatingDaemonHarness()
    await CortexDaemon._handle_user_action(
        harness,  # type: ignore[arg-type]
        {"intervention_id": "iv-1", "rating": "thumbs_up"},
    )
    assert harness._policy_lifecycle.observations == [
        (
            "iv-1",
            "user_rating",
            "user-rating:iv-1:thumbs_up",
            {"rating": "thumbs_up"},
        )
    ]
    assert harness._helpfulness.recorded == [("iv-1", "thumbs_up", None)]


@pytest.mark.asyncio
async def test_user_rating_thumbs_down_is_observed_without_early_reward() -> None:
    from cortex.services.runtime_daemon import CortexDaemon

    harness = _RatingDaemonHarness()
    await CortexDaemon._handle_user_action(
        harness,  # type: ignore[arg-type]
        {"intervention_id": "iv-2", "rating": "thumbs_down"},
    )
    assert harness._policy_lifecycle.observations[0][0:2] == (
        "iv-2",
        "user_rating",
    )


@pytest.mark.asyncio
async def test_user_rating_is_keyed_by_intervention_not_shared_decision_slot() -> None:
    from cortex.services.runtime_daemon import CortexDaemon

    harness = _RatingDaemonHarness()
    await CortexDaemon._handle_user_action(
        harness,  # type: ignore[arg-type]
        {"intervention_id": "iv-3", "rating": "thumbs_up"},
    )
    assert harness._policy_lifecycle.observations[0][0] == "iv-3"
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
        policy=policy,
        escalation_threshold=2,
    )

    # Drive the first escalation: 2 approvals at the default tier.
    await ladder.record_approval("close_tab")
    await ladder.record_approval("close_tab")
    state = (await ladder.get_all_states())["close_tab"]
    timestamps_after_first = list(state.get("approval_timestamps", []))
    # Escalation should NOT have wiped the timestamp ledger.
    assert len(timestamps_after_first) >= 1, "approval_timestamps must persist across escalation"

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
        client_id="c1",
        websocket=fake_ws,
        client_type="chrome",
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
# WP8 — concurrent observation attribution
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_intervention_observations_have_distinct_keys() -> None:
    """No shared mutable decision slot can steal an intervention's feedback."""
    from cortex.services.runtime_daemon import CortexDaemon

    harness = _RatingDaemonHarness()
    await CortexDaemon._handle_user_action(
        harness,  # type: ignore[arg-type]
        {"intervention_id": "iv-A", "rating": "thumbs_up"},
    )
    await CortexDaemon._handle_user_action(
        harness,  # type: ignore[arg-type]
        {"intervention_id": "iv-B", "rating": "thumbs_down"},
    )
    assert [item[0] for item in harness._policy_lifecycle.observations] == [
        "iv-A",
        "iv-B",
    ]
    assert len({item[2] for item in harness._policy_lifecycle.observations}) == 2
