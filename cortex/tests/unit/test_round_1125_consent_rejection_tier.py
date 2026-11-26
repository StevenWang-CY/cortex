"""Rejections earned at an earlier consent tier must not demote a new tier.

``record_rejection`` de-escalates after "3 rejections at current level", but
escalation reset only the approvals counter. Rejections left over from before
a promotion (and old enough to have stopped blocking it) therefore counted at
the new tier, so a single dismissal right after earning it dropped it again.
"""

from __future__ import annotations

import asyncio

from cortex.application.clock import FakeClock
from cortex.services.consent.ladder import PREVIEW, REVERSIBLE_ACT, ConsentLadder
from cortex.services.consent.policy import ConsentPolicy

_DAY_MS = 86_400_000


def test_rejections_from_an_earlier_tier_do_not_count_at_the_new_tier() -> None:
    async def scenario() -> tuple[int, int]:
        clock = FakeClock(wall_unix_ms=1_000)
        ladder = ConsentLadder(policy=ConsentPolicy(), store=None, clock=clock)
        await ladder.record_rejection("close_tab")
        await ladder.record_rejection("close_tab")
        # Let the rejections age out of the 30-day recency window so they no
        # longer block escalation, then earn the next tier.
        clock.advance(wall_ms=31 * _DAY_MS)
        for _ in range(5):
            await ladder.record_approval("close_tab")
        promoted = await ladder.get_level("close_tab")
        await ladder.record_rejection("close_tab")
        return promoted, await ladder.get_level("close_tab")

    promoted, after_one_rejection = asyncio.run(scenario())
    assert promoted == REVERSIBLE_ACT
    assert after_one_rejection == REVERSIBLE_ACT


def test_three_rejections_at_the_new_tier_still_de_escalate() -> None:
    async def scenario() -> int:
        clock = FakeClock(wall_unix_ms=1_000)
        ladder = ConsentLadder(policy=ConsentPolicy(), store=None, clock=clock)
        for _ in range(5):
            await ladder.record_approval("close_tab")
        assert await ladder.get_level("close_tab") == REVERSIBLE_ACT
        for _ in range(3):
            await ladder.record_rejection("close_tab")
        return await ladder.get_level("close_tab")

    assert asyncio.run(scenario()) == PREVIEW
