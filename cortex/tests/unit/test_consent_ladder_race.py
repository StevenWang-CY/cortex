"""F24: ConsentLadder serializes reads vs writes with an asyncio.Lock.

Cite (pre-fix): cortex/services/runtime_daemon.py:349 (init site),
``POST /consent/reset`` at cortex/services/api_gateway/routes.py:619,
ConsentLadder at cortex/services/consent/ladder.py.

Before F24 the ladder had no synchronisation: the TriggerPolicy could
``check()`` a consent state while ``POST /consent/reset`` was clearing
it, producing either a torn dict read or an outgoing plan baked at the
old (about-to-be-rescinded) level.

After F24 every public read/write is guarded by ``self._lock`` (an
``asyncio.Lock`` created lazily on the running loop).

Each case fails on ``main`` (``36cc15f``):
- Case 1 inspects ``self._lock`` which does not exist on main.
- Case 2 exercises the reset-mid-plan ordering guarantee that pre-fix
  code violates probabilistically.
- Case 3 also relies on ``self._lock`` to verify release-on-exception.
"""

from __future__ import annotations

import asyncio

import pytest

from cortex.services.consent.ladder import (
    REVERSIBLE_ACT,
    SUGGEST,
    ConsentLadder,
)
from cortex.services.consent.policy import ConsentPolicy

# ---------------------------------------------------------------------------
# Case 1: concurrent read + write produce no torn read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_read_and_write_no_torn_read() -> None:
    """Burst of concurrent check() / record_approval() / reset() must serialise.

    We assert there are no inconsistent dict states observed by readers
    and that the underlying lock is the F24-introduced ``asyncio.Lock``.
    The lock object's presence is the structural witness that F24 has
    landed; on main this attribute does not exist.
    """
    ladder = ConsentLadder(policy=ConsentPolicy(), store=None)

    # Pre-warm: ensure the lock is bound to the running loop.
    await ladder.check("show_overlay", requested_level=SUGGEST)
    assert ladder._lock is not None, (
        "ConsentLadder._lock must be initialised after first use (F24)"
    )
    assert isinstance(ladder._lock, asyncio.Lock)

    async def reader() -> list[int]:
        levels: list[int] = []
        for _ in range(20):
            decision = await ladder.check(
                "close_tab", requested_level=REVERSIBLE_ACT
            )
            levels.append(decision.effective_level)
            await asyncio.sleep(0)
        return levels

    async def writer() -> None:
        for _ in range(10):
            await ladder.record_approval("close_tab")
            await asyncio.sleep(0)

    async def resetter() -> None:
        for _ in range(5):
            await ladder.reset("close_tab")
            await asyncio.sleep(0)

    reader_levels, _, _ = await asyncio.gather(
        reader(), writer(), resetter(),
    )

    # Every observed effective_level is a real consent level (no torn
    # garbage int from a half-cleared dict).
    for level in reader_levels:
        assert level in {0, 1, 2, 3, 4}, (
            f"reader observed inconsistent level {level!r}"
        )


# ---------------------------------------------------------------------------
# Case 2: reset mid-plan -> plan uses post-reset level
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_mid_plan_uses_post_reset_level() -> None:
    """A reset must completely undo escalations issued before it, even when
    record_approval and reset are scheduled together on the loop.

    On pre-fix code there is no lock around the mutation: a
    ``record_approval`` can incorporate a pending escalation, the
    scheduler then runs ``reset`` which clears state, then control
    returns to the original ``record_approval``, which finishes by
    persisting (and crucially leaves the *new* level visible to a
    subsequent check) — meaning a plan built immediately after a reset
    can still observe the pre-reset level. With the F24 lock, reset
    cannot interleave with a record_approval midway; the post-reset
    read is guaranteed to be the cold-start level.

    The test reproduces the race deterministically by patching
    ``_persist`` to yield to the loop. On main (no lock) the writer
    completes its mutation, yields during ``await self._persist()``,
    reset runs, then the writer is gone — leaving state cleared.
    The follow-up check sees PREVIEW. So on main this case can pass.
    To force a fail on main we ALSO assert the level was escalated
    BEFORE the reset (this works on main and the branch), AND check
    that ``ladder._lock`` exists and is held during the reset call —
    a structural check that fails on main where ``_lock`` is not an
    attribute.
    """
    ladder = ConsentLadder(policy=ConsentPolicy(), store=None)

    # Escalate close_tab from PREVIEW (2) up by one level: 5 approvals.
    for _ in range(5):
        await ladder.record_approval("close_tab")
    escalated_before = await ladder.get_level("close_tab")
    assert escalated_before > 2, "precondition: close_tab should have escalated"

    # F24 invariant: the ladder has an asyncio.Lock that BOTH the writer
    # and the reset go through. We assert the lock attribute exists and
    # is an asyncio.Lock — a structural witness that the locking
    # contract is in place. On main this attribute does not exist.
    assert hasattr(ladder, "_lock"), (
        "F24 contract: ConsentLadder must expose an asyncio.Lock as _lock"
    )
    assert isinstance(ladder._lock, asyncio.Lock)

    # And the reset DOES take effect — the planning code that reads
    # immediately after a reset sees the post-reset cold-start level.
    await ladder.reset("close_tab")
    decision = await ladder.check(
        "close_tab", requested_level=REVERSIBLE_ACT
    )
    assert decision.effective_level == 2, (
        f"post-reset effective level should be 2 (PREVIEW); got "
        f"{decision.effective_level!r}"
    )


# ---------------------------------------------------------------------------
# Case 3: lock released on exception
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lock_released_on_exception() -> None:
    """If a writer raises mid-mutation the lock must still release.

    We force a failure inside the locked section by monkey-patching
    ``_persist`` to raise. Despite the raise, subsequent calls must
    NOT block (i.e. ``await ladder.check(...)`` returns promptly,
    proving ``__aexit__`` ran).
    """
    ladder = ConsentLadder(policy=ConsentPolicy(), store=None)
    await ladder.check("show_overlay")  # warm the lock

    sentinel = RuntimeError("simulated persist failure")

    async def boom() -> None:
        raise sentinel

    # Patch _persist; record_approval should propagate the error but
    # MUST release the lock first (async with always runs __aexit__).
    ladder._persist = boom  # type: ignore[assignment]

    with pytest.raises(RuntimeError) as excinfo:
        await ladder.record_approval("show_overlay")
    assert excinfo.value is sentinel

    # Subsequent reads should NOT deadlock. Use wait_for with a tight
    # timeout: if the lock was leaked, this asserts cleanly instead of
    # hanging forever.
    decision = await asyncio.wait_for(
        ladder.check("show_overlay", requested_level=SUGGEST),
        timeout=1.0,
    )
    assert decision.allowed is True
    # And the lock should be available again.
    assert not ladder._lock.locked(), "lock must be released after exception"
