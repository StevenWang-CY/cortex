"""Recency/decay consent ladder behavior introduced in 0.2.0."""

from __future__ import annotations

import asyncio

from cortex.services.consent.ladder import PREVIEW, ConsentLadder
from cortex.services.consent.policy import ConsentPolicy


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_old_approvals_outside_30_day_window_do_not_escalate(monkeypatch):
    policy = ConsentPolicy()
    ladder = ConsentLadder(policy=policy, store=None)

    start = 1_700_000_000.0
    monkeypatch.setattr("cortex.services.consent.ladder.time.time", lambda: start)

    for _ in range(4):
        _run(ladder.record_approval("close_tab"))

    # Move beyond recency window; only the latest approval should count.
    monkeypatch.setattr("cortex.services.consent.ladder.time.time", lambda: start + 31 * 24 * 3600)
    _run(ladder.record_approval("close_tab"))

    assert _run(ladder.get_level("close_tab")) == PREVIEW


def test_recent_rejection_blocks_escalation_until_window_expires(monkeypatch):
    policy = ConsentPolicy()
    ladder = ConsentLadder(policy=policy, store=None)

    t0 = 1_710_000_000.0
    monkeypatch.setattr("cortex.services.consent.ladder.time.time", lambda: t0)
    _run(ladder.record_rejection("close_tab"))

    for _ in range(5):
        _run(ladder.record_approval("close_tab"))
    assert _run(ladder.get_level("close_tab")) == PREVIEW

    # Rejection ages out; approvals can now escalate.
    t1 = t0 + 31 * 24 * 3600
    monkeypatch.setattr("cortex.services.consent.ladder.time.time", lambda: t1)
    for _ in range(5):
        _run(ladder.record_approval("close_tab"))

    assert _run(ladder.get_level("close_tab")) == PREVIEW + 1
