"""Recency/decay consent ladder behavior introduced in 0.2.0."""

from __future__ import annotations

import asyncio

from cortex.application.clock import FakeClock
from cortex.services.consent.ladder import PREVIEW, ConsentLadder
from cortex.services.consent.policy import ConsentPolicy

_LOOP = asyncio.new_event_loop()


def _run(coro):
    # Dedicated module loop, NOT asyncio.get_event_loop() (deprecated; it
    # returns a closed/foreign loop once pytest-asyncio tears down the
    # default loop earlier in the same process, which made these tests fail
    # only when run alongside async tests — CLAUDE.md rule #16).
    return _LOOP.run_until_complete(coro)


def test_old_approvals_outside_30_day_window_do_not_escalate():
    policy = ConsentPolicy()
    clock = FakeClock(wall_unix_ms=1_700_000_000_000)
    ladder = ConsentLadder(policy=policy, store=None, clock=clock)

    for _ in range(4):
        _run(ladder.record_approval("close_tab"))

    # Move beyond recency window; only the latest approval should count.
    clock.advance(wall_ms=31 * 24 * 3600 * 1000)
    _run(ladder.record_approval("close_tab"))

    assert _run(ladder.get_level("close_tab")) == PREVIEW


def test_recent_rejection_blocks_escalation_until_window_expires():
    policy = ConsentPolicy()
    clock = FakeClock(wall_unix_ms=1_710_000_000_000)
    ladder = ConsentLadder(policy=policy, store=None, clock=clock)
    _run(ladder.record_rejection("close_tab"))

    for _ in range(5):
        _run(ladder.record_approval("close_tab"))
    assert _run(ladder.get_level("close_tab")) == PREVIEW

    # Rejection ages out; approvals can now escalate.
    clock.advance(wall_ms=31 * 24 * 3600 * 1000)
    for _ in range(5):
        _run(ladder.record_approval("close_tab"))

    assert _run(ladder.get_level("close_tab")) == PREVIEW + 1
