"""
P0 §3.6 — F16 atomic-swap step-state preservation.

When the daemon broadcasts a fresh intervention payload that carries
the SAME ``intervention_id`` as the currently active plan (F16 atomic
swap — a re-rendered plan replacing the stale one), the user's
checkbox state on the overlay MUST survive. The naive
``plan.micro_steps = new`` assignment would wipe every
``status="done"`` set by the user since the previous emission.

The merge helper in :mod:`cortex.services.intervention_engine.restore`
implements the rule:
  * same length + identical text per index → preserve OLD entry
    (its status, started_at, completed_at win)
  * length mismatch → use NEW verbatim
  * length match but text differs at index i → use NEW at i
    (the LLM rewrote the instruction; the prior tick is no longer
    semantically valid)
"""

from __future__ import annotations

from datetime import datetime, timezone

from cortex.libs.schemas.intervention import MicroStep
from cortex.services.intervention_engine.restore import merge_micro_steps


def _step(text: str, status: str = "pending") -> MicroStep:
    return MicroStep(text=text, status=status)  # type: ignore[arg-type]


def test_same_texts_preserve_done_status() -> None:
    """The classic F16 swap case: the daemon re-emits the same plan
    while the user has step 0 ticked. The merge must keep
    ``status="done"`` on the surviving entry."""
    old = [_step("a", "done"), _step("b", "pending")]
    new = [_step("a", "pending"), _step("b", "pending")]
    merged = merge_micro_steps(old, new)
    assert merged[0].status == "done"
    assert merged[1].status == "pending"


def test_text_change_resets_to_new_entry() -> None:
    """If the LLM rewrote step 0, the tick on the prior version is no
    longer semantically valid. The merge must take the NEW entry —
    which resets the status to whatever the LLM emitted (typically
    ``"pending"``)."""
    old = [_step("a", "done"), _step("b", "pending")]
    new = [_step("a-revised", "pending"), _step("b", "pending")]
    merged = merge_micro_steps(old, new)
    assert merged[0].text == "a-revised"
    assert merged[0].status == "pending"
    # Step 1 unchanged: same text → preserve old (which was pending
    # anyway).
    assert merged[1].text == "b"
    assert merged[1].status == "pending"


def test_length_mismatch_uses_new_verbatim() -> None:
    """A change in step count is a structurally different plan —
    preserve nothing, use the new list verbatim."""
    old = [_step("a", "done"), _step("b", "done"), _step("c", "done")]
    new = [_step("a", "pending"), _step("b", "pending")]
    merged = merge_micro_steps(old, new)
    assert len(merged) == 2
    assert all(s.status == "pending" for s in merged)


def test_preserves_timestamps_on_match() -> None:
    """When the merge picks the OLD entry, all of its lifecycle
    metadata (started_at, completed_at) must survive too — those are
    needed for the session-report timeline."""
    started = datetime(2026, 5, 25, 10, 0, tzinfo=timezone.utc)
    completed = datetime(2026, 5, 25, 10, 5, tzinfo=timezone.utc)
    old = [MicroStep(text="a", status="done", started_at=started, completed_at=completed)]
    new = [_step("a", "pending")]
    merged = merge_micro_steps(old, new)
    assert merged[0].status == "done"
    assert merged[0].started_at == started
    assert merged[0].completed_at == completed


def test_empty_lists() -> None:
    """Two empty lists merge to an empty list — defensive guard."""
    assert merge_micro_steps([], []) == []


def test_skipped_status_preserved() -> None:
    """The ``"skipped"`` literal is part of the catalog and must
    survive a swap the same way ``"done"`` does."""
    old = [_step("a", "skipped")]
    new = [_step("a", "pending")]
    merged = merge_micro_steps(old, new)
    assert merged[0].status == "skipped"
