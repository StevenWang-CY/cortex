"""Consent de-escalation must not lock an action out of user confirmation.

The daemon records a consent *rejection* for every gated action type of a
dismissed proposal, and an *approval* only after a verified receipt of an
exactly authorized action. Authorization itself is decided by
``ConsentLadder.exact_decision_scope``, which permits a request only when
``policy minimum <= requested <= earned level``.
"""

from __future__ import annotations

import asyncio

from cortex.application.clock import FakeClock
from cortex.services.consent.ladder import PREVIEW, ConsentLadder
from cortex.services.consent.policy import ConsentPolicy


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def test_three_dismissals_do_not_make_a_previewed_action_unauthorizable() -> None:
    """Dismissing three proposals that contained tab grouping dropped the
    earned level to SUGGEST, below the action's PREVIEW floor. From then on a
    user-confirmed PREVIEW authorization for ``group_tabs`` was denied, and
    because approvals are only recorded after a verified (i.e. authorized)
    receipt, nothing could ever raise the level again."""

    async def scenario() -> tuple[bool, bool, int]:
        ladder = ConsentLadder(policy=ConsentPolicy(), store=None, clock=FakeClock(wall_unix_ms=1))
        before = (await ladder.check("group_tabs", requested_level=PREVIEW)).allowed
        for _ in range(3):
            await ladder.record_rejection("group_tabs")
        after = (await ladder.check("group_tabs", requested_level=PREVIEW)).allowed
        return before, after, await ladder.get_level("group_tabs")

    before, after, level = _run(scenario())
    assert before is True
    assert after is True, "three dismissals permanently denied user-confirmed tab grouping"
    assert level == PREVIEW
