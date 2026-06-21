"""ITEM 3 — LEETCODE-CAPS: every advertised capability is reachable.

Asserts that every action literal in ``LeetCodeAdapter._CAPABILITIES`` is
actually emitted by at least one ``InterventionMatrix`` intervention's
``build_action()`` method.  Before the prune (15 capabilities) this fails
with 10 orphaned action literals; after (5 capabilities) it passes.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from cortex.libs.adapters.leetcode_adapter import LeetCodeAdapter
from cortex.libs.schemas.leetcode import (
    LeetCodeContext,
    LeetCodeMode,
    LeetCodeModeEstimate,
    LeetCodeStage,
)
from cortex.services.intervention_engine.leetcode_interventions import (
    AmygdalaLockout,
    PatternLadder,
    RestatementScratchpad,
    SolutionEscapeFriction,
    SubmissionDisciplineGuard,
)

# ---------------------------------------------------------------------------
# Helper — build mode estimates that trigger each intervention
# ---------------------------------------------------------------------------

def _mode_estimate(
    stage: LeetCodeStage,
    mode: LeetCodeMode,
    pathway: str = "comprehension",
) -> LeetCodeModeEstimate:
    """Construct a minimal LeetCodeModeEstimate for the given stage/mode."""
    est = MagicMock(spec=LeetCodeModeEstimate)
    est.stage = stage
    est.mode = mode
    # destructive sub-object used by RestatementScratchpad
    est.destructive = MagicMock()
    est.destructive.pathway = pathway
    return est  # type: ignore[return-value]


def _context(**kwargs: Any) -> LeetCodeContext:
    """Build a LeetCodeContext with sensible defaults, overridden by kwargs."""
    defaults: dict[str, Any] = {
        "problem_id": "1",
        "title": "Two Sum",
        "difficulty": "Easy",
        "stage": LeetCodeStage.READ,
        "wrong_answer_count": 5,       # above SubmissionDisciplineGuard threshold
        "submission_count": 6,
        "solutions_tab_attempted": True,
        "time_elapsed_s": 0.0,
        "code_snapshot": "",
        "tags": [],
        "last_submission_result": "Wrong Answer",
    }
    defaults.update(kwargs)
    return LeetCodeContext(**defaults)


# ---------------------------------------------------------------------------
# Collect every action literal reachable from InterventionMatrix
# ---------------------------------------------------------------------------

def _collect_reachable_actions() -> set[str]:
    """Force each intervention to fire and collect all emitted action keys."""
    reachable: set[str] = set()

    # RestatementScratchpad: READ + DESTRUCTIVE_STRUGGLE + pathway=comprehension
    r = RestatementScratchpad()
    est = _mode_estimate(LeetCodeStage.READ, LeetCodeMode.DESTRUCTIVE_STRUGGLE)
    ctx = _context(stage=LeetCodeStage.READ)
    action = r.build_action(est, ctx)
    reachable.add(action["action"])

    # PatternLadder: PLAN + PRODUCTIVE_STRUGGLE
    pl = PatternLadder()
    est2 = _mode_estimate(LeetCodeStage.PLAN, LeetCodeMode.PRODUCTIVE_STRUGGLE)
    ctx2 = _context(stage=LeetCodeStage.PLAN)
    action2 = pl.build_action(est2, ctx2)
    reachable.add(action2["action"])

    # AmygdalaLockout: DEBUG + AMYGDALA_HIJACK
    al = AmygdalaLockout()
    est3 = _mode_estimate(LeetCodeStage.DEBUG, LeetCodeMode.AMYGDALA_HIJACK)
    ctx3 = _context(stage=LeetCodeStage.DEBUG)
    action3 = al.build_action(est3, ctx3)
    reachable.add(action3["action"])

    # SubmissionDisciplineGuard: IMPLEMENT + any mode + wrong_answer_count > 2
    sdg = SubmissionDisciplineGuard()
    est4 = _mode_estimate(LeetCodeStage.IMPLEMENT, LeetCodeMode.FLOW)
    ctx4 = _context(stage=LeetCodeStage.IMPLEMENT, wrong_answer_count=5)
    action4 = sdg.build_action(est4, ctx4)
    reachable.add(action4["action"])

    # SolutionEscapeFriction: any stage + PANIC + solutions_tab_attempted=True
    sef = SolutionEscapeFriction()
    est5 = _mode_estimate(LeetCodeStage.IMPLEMENT, LeetCodeMode.PANIC)
    ctx5 = _context(stage=LeetCodeStage.IMPLEMENT, solutions_tab_attempted=True)
    action5 = sef.build_action(est5, ctx5)
    reachable.add(action5["action"])

    return reachable


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_all_capabilities_are_reachable() -> None:
    """Every action in LeetCodeAdapter._CAPABILITIES is emitted by at least one intervention."""
    adapter_caps = set(LeetCodeAdapter._CAPABILITIES)
    reachable = _collect_reachable_actions()

    orphaned = adapter_caps - reachable
    assert not orphaned, (
        f"The following capabilities are advertised in LeetCodeAdapter._CAPABILITIES "
        f"but are never emitted by any InterventionMatrix.build_action(): {sorted(orphaned)}"
    )


def test_no_unregistered_actions_emitted() -> None:
    """Every action emitted by InterventionMatrix is registered in _CAPABILITIES.

    This is the inverse check: the interventions don't emit actions the adapter
    doesn't know about (which would be silently rejected by execute()).
    """
    adapter_caps = set(LeetCodeAdapter._CAPABILITIES)
    reachable = _collect_reachable_actions()

    unknown = reachable - adapter_caps
    assert not unknown, (
        f"InterventionMatrix emits action(s) {sorted(unknown)!r} that are not "
        f"registered in LeetCodeAdapter._CAPABILITIES."
    )


def test_capabilities_count() -> None:
    """Exactly 5 capabilities after pruning the 10 unimplemented ones."""
    assert len(LeetCodeAdapter._CAPABILITIES) == 5, (
        f"Expected 5 capabilities, got {len(LeetCodeAdapter._CAPABILITIES)}: "
        f"{LeetCodeAdapter._CAPABILITIES}"
    )
