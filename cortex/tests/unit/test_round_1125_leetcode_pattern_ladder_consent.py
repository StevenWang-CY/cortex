"""The LeetCode pattern-ladder hint must be able to pass the consent ladder.

``PatternLadder.build_action`` requests ``required_consent_level="suggest"``
and ``_maybe_trigger_leetcode_interventions`` maps that to level 1 before
``ConsentLadder.check``. ``show_pattern_ladder`` is not in the consent
policy's action table, so its minimum falls back to PREVIEW (2), and the
ladder permits a request only when ``minimum <= requested``. A SUGGEST
request for it is therefore denied on every call, for every user: the hint
ladder for productive struggle can never be shown.
"""

from __future__ import annotations

import asyncio

from cortex.services.consent.ladder import SUGGEST, ConsentLadder
from cortex.services.consent.policy import ConsentPolicy
from cortex.services.intervention_engine.leetcode_interventions import PatternLadder

_LEVEL = {"observe": 0, "suggest": 1, "preview": 2, "reversible_act": 3, "autonomous_act": 4}


def test_pattern_ladder_requests_a_level_its_policy_minimum_admits() -> None:
    from cortex.libs.schemas.leetcode import (
        LeetCodeContext,
        LeetCodeMode,
        LeetCodeModeEstimate,
        LeetCodeStage,
    )

    estimate = LeetCodeModeEstimate(
        stage=LeetCodeStage.PLAN,
        mode=LeetCodeMode.PRODUCTIVE_STRUGGLE,
    )
    context = LeetCodeContext(problem_id="1", title="Two Sum", stage=LeetCodeStage.PLAN)
    ladder_cell = PatternLadder()
    assert ladder_cell.should_trigger(estimate, context)
    action = ladder_cell.build_action(estimate, context)
    requested = _LEVEL[str(action["required_consent_level"])]
    assert requested == SUGGEST

    async def decide() -> bool:
        ladder = ConsentLadder(policy=ConsentPolicy(), store=None)
        decision = await ladder.check(str(action["action"]), requested_level=requested)
        return decision.allowed

    assert asyncio.run(decide()) is True, "show_pattern_ladder is denied for every user"
