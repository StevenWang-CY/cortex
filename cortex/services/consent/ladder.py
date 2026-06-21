"""
Consent Engine — Consent Ladder

Implements the formalized consent hierarchy:
    OBSERVE → SUGGEST → PREVIEW → REVERSIBLE_ACT → AUTONOMOUS_ACT

Cortex is forbidden from executing autonomous actions until the user
has manually approved that specific intervention type multiple times.

Each action type has its own consent level that escalates independently
based on the user's approval history.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from typing import Any

from cortex.services.consent.policy import ConsentPolicy

logger = logging.getLogger(__name__)

# Consent level constants
OBSERVE = 0
SUGGEST = 1
PREVIEW = 2
REVERSIBLE_ACT = 3
AUTONOMOUS_ACT = 4

_LEVEL_NAMES = {
    0: "observe",
    1: "suggest",
    2: "preview",
    3: "reversible_act",
    4: "autonomous_act",
}

# Default approvals needed to escalate to next level
_DEFAULT_ESCALATION_THRESHOLD = 5
_RECENCY_WINDOW_SECONDS = 30 * 24 * 3600
_DECAY_HALF_LIFE_SECONDS = 10 * 24 * 3600


class ConsentDecision:
    """Result of a consent check."""

    __slots__ = ("allowed", "effective_level", "requested_level", "reason")

    def __init__(
        self,
        allowed: bool,
        effective_level: int,
        requested_level: int,
        reason: str = "",
    ) -> None:
        self.allowed = allowed
        self.effective_level = effective_level
        self.requested_level = requested_level
        self.reason = reason


class ConsentLadder:
    """
    Manages consent levels for each action type.

    Levels escalate after N manual approvals. The user's trust is
    built up gradually — Cortex earns autonomy, never assumes it.

    Usage:
        ladder = ConsentLadder(policy=ConsentPolicy(), store=redis_store)
        decision = await ladder.check("close_tab", requested_level=REVERSIBLE_ACT)
        if decision.allowed:
            execute_action()
            await ladder.record_approval("close_tab")
        else:
            downgrade_to(decision.effective_level)
    """

    def __init__(
        self,
        policy: ConsentPolicy | None = None,
        store: Any = None,
        escalation_threshold: int = _DEFAULT_ESCALATION_THRESHOLD,
    ) -> None:
        self._policy = policy or ConsentPolicy()
        self._store = store
        self._escalation_threshold = escalation_threshold

        # In-memory state: action_type → {level, approvals, rejections}
        self._action_states: dict[str, dict[str, Any]] = {}
        self._loaded = False

        # F24: serialize concurrent reads and writes against the
        # in-memory state. The TriggerPolicy reads consent levels while
        # ``POST /consent/reset`` may be clearing them in parallel; the
        # pre-fix code had no synchronisation, so a reset arriving
        # mid-plan-construction could leave a partially-reset state
        # dict visible to the in-flight planner.
        #
        # Phase-4b TASK H: bind the lock at construction. Python 3.10+
        # ``asyncio.Lock()`` no longer binds to a specific event loop
        # at construction — it lazily picks the running loop on the
        # first ``__aenter__`` / ``__aexit__``. The legacy lazy
        # ``_get_lock()`` shim was therefore redundant. Cross-thread
        # callers don't exist today (consent state is only mutated by
        # the daemon's event loop); if that changes, swap to
        # ``threading.RLock`` and adjust the ``async with`` blocks
        # below.
        self._lock: asyncio.Lock = asyncio.Lock()

    def _get_lock(self) -> asyncio.Lock:
        """Return the lock bound to the running event loop (F24).

        Phase-4b TASK H: retained as a thin wrapper for callers that
        already referenced it. New code should use ``self._lock``
        directly.
        """
        return self._lock

    async def _ensure_loaded(self) -> None:
        """Load consent state from store if available.

        P1-BE-CONSENT-RACE: the load runs under the SAME ``self._lock``
        that the mutating methods (``record_approval``/``record_rejection``
        /``reset``) hold, and ``self._loaded`` is flipped to ``True`` only
        AFTER the awaited store read completes. The pre-fix code set
        ``self._loaded = True`` *before* the ``await`` and *outside* the
        lock, so two concurrent first-callers interleaved:

          * caller A enters, sees ``_loaded`` False, sets it True, awaits
            ``get_json`` (yields control);
          * caller B (e.g. a ``record_approval`` that already mutated
            ``_action_states``) runs to completion;
          * caller A resumes and *overwrites* ``_action_states`` with the
            stale ``get_json`` payload — silently dropping B's recorded
            approval. Consent gates autonomous workspace mutation, so a
            lost approval is a correctness/safety bug.

        Acquiring the lock here serialises the load against every mutator.
        Callers MUST invoke this once at the top of each public entrypoint
        *before* taking ``self._lock`` themselves — never while already
        holding it (``asyncio.Lock`` is not re-entrant; that would
        deadlock). The double check inside the lock makes a concurrent
        second caller a cheap no-op once the first has loaded.
        """
        if self._loaded:
            return
        async with self._lock:
            # Re-check under the lock: a racing caller may have completed
            # the load while we were waiting to acquire it.
            if self._loaded:
                return
            if self._store is None:
                self._loaded = True
                return
            try:
                data = await self._store.get_json("consent_ladder_state")
                if data and isinstance(data, dict):
                    self._action_states = data.get("action_states", {})
                    if "global_max" in data:
                        self._policy.global_max_level = data["global_max"]
            except Exception:
                logger.debug("No stored consent state found, using defaults")
            # Flip the flag only after the await resolves so a concurrent
            # caller cannot observe ``_loaded`` True against an as-yet
            # unpopulated ``_action_states``.
            self._loaded = True

    async def _persist(self) -> None:
        """Persist consent state to store."""
        if self._store is None:
            return
        try:
            await self._store.set_json("consent_ladder_state", {
                "action_states": self._action_states,
                "global_max": self._policy.global_max_level,
            })
        except Exception:
            logger.exception("Failed to persist consent state")

    def _get_state(self, action_type: str) -> dict[str, Any]:
        """Get or create state for an action type."""
        if action_type not in self._action_states:
            self._action_states[action_type] = {
                "level": self._policy.get_minimum_level(action_type),
                "approvals": 0,
                "rejections": 0,
                "total_approvals": 0,
                "last_approval": None,
                "last_rejection": None,
                "approval_timestamps": [],
                "rejection_timestamps": [],
            }
        return self._action_states[action_type]

    async def check(
        self,
        action_type: str,
        requested_level: int = REVERSIBLE_ACT,
    ) -> ConsentDecision:
        """
        Check if an action is allowed at the requested consent level.

        Args:
            action_type: Type of action (close_tab, fold_code, etc.)
            requested_level: Desired consent level.

        Returns:
            ConsentDecision with allowed/denied and effective level.
        """
        await self._ensure_loaded()
        # F24: read the state under the lock so a concurrent reset()
        # cannot leave us looking at a half-cleared dict.
        async with self._get_lock():
            state = self._get_state(action_type)
            current_level = state["level"]
            min_level = self._policy.get_minimum_level(action_type)
            global_max = self._policy.global_max_level

        # Effective level is capped by global max and current earned level
        max_allowed = min(current_level, global_max)

        if requested_level <= max_allowed:
            return ConsentDecision(
                allowed=True,
                effective_level=requested_level,
                requested_level=requested_level,
                reason=f"Action '{action_type}' allowed at level {_LEVEL_NAMES.get(requested_level, '?')}",
            )

        # Downgrade to what's allowed
        effective = min(max_allowed, requested_level)
        return ConsentDecision(
            allowed=effective >= min_level,
            effective_level=effective,
            requested_level=requested_level,
            reason=(
                f"Action '{action_type}' downgraded from "
                f"{_LEVEL_NAMES.get(requested_level, '?')} to "
                f"{_LEVEL_NAMES.get(effective, '?')} "
                f"(earned: {_LEVEL_NAMES.get(current_level, '?')}, "
                f"cap: {_LEVEL_NAMES.get(global_max, '?')})"
            ),
        )

    async def record_approval(self, action_type: str) -> None:
        """
        Record that the user approved an action.

        After enough approvals, the consent level escalates.
        """
        await self._ensure_loaded()
        # F24: mutate the state under the lock so a concurrent
        # ``record_rejection`` or ``reset`` cannot interleave with the
        # escalation check and leave the dict in an inconsistent shape.
        async with self._get_lock():
            state = self._get_state(action_type)
            state["approvals"] += 1
            state["total_approvals"] += 1
            state["last_approval"] = time.time()
            state.setdefault("approval_timestamps", []).append(state["last_approval"])
            self._prune_old_timestamps(state)

            # Check for escalation (recency-weighted trust, no recent reversals).
            #
            # Phase-4b TASK H: gate the threshold on the per-tier
            # ``approvals`` counter (resets to 0 on escalation) rather
            # than ``len(approval_timestamps)`` (which now persists
            # across tiers to preserve the 30-day decay window). The
            # timestamps list is still used for the recency-weighted
            # trust factor, but only the counter decides "have we
            # earned the next tier yet?".
            now = time.time()
            weighted_approvals = self._weighted_recent_approvals(state, now)
            recent_rejections = len(state.get("rejection_timestamps", []))
            tier_approvals = int(state["approvals"])
            if (
                tier_approvals >= self._escalation_threshold
                and weighted_approvals >= (self._escalation_threshold * 0.8)
                and recent_rejections == 0
                and state["level"] < AUTONOMOUS_ACT
            ):
                old_level = state["level"]
                state["level"] = min(state["level"] + 1, AUTONOMOUS_ACT)
                # Phase-4b TASK H: reset only the approvals counter so
                # the next escalation tier earns credit from scratch —
                # do NOT clear ``approval_timestamps``. The 30-day
                # decay window is the user's trust history and must
                # survive across tier promotions; otherwise two
                # consecutive escalations would silently bleed
                # earned credit and ratchet the user back to suggest.
                state["approvals"] = 0
                logger.info(
                    "Consent escalated for '%s': %s -> %s (after %d approvals)",
                    action_type,
                    _LEVEL_NAMES.get(old_level, "?"),
                    _LEVEL_NAMES.get(state["level"], "?"),
                    self._escalation_threshold,
                )

            await self._persist()

    async def record_rejection(self, action_type: str) -> None:
        """
        Record that the user rejected an action.

        Rejections count against the approval progress.
        """
        await self._ensure_loaded()
        async with self._get_lock():
            state = self._get_state(action_type)
            state["rejections"] += 1
            state["last_rejection"] = time.time()
            state.setdefault("rejection_timestamps", []).append(state["last_rejection"])
            self._prune_old_timestamps(state)

            # 3 rejections at current level -> de-escalate
            if state["rejections"] >= 3 and state["level"] > SUGGEST:
                old_level = state["level"]
                state["level"] = max(state["level"] - 1, SUGGEST)
                state["rejections"] = 0
                state["approvals"] = 0
                logger.info(
                    "Consent de-escalated for '%s': %s -> %s (after 3 rejections)",
                    action_type,
                    _LEVEL_NAMES.get(old_level, "?"),
                    _LEVEL_NAMES.get(state["level"], "?"),
                )

            await self._persist()

    def _prune_old_timestamps(self, state: dict[str, Any]) -> None:
        now = time.time()
        cutoff = now - _RECENCY_WINDOW_SECONDS
        approvals = [t for t in state.get("approval_timestamps", []) if t >= cutoff]
        rejections = [t for t in state.get("rejection_timestamps", []) if t >= cutoff]
        state["approval_timestamps"] = approvals
        state["rejection_timestamps"] = rejections

    def _weighted_recent_approvals(self, state: dict[str, Any], now: float) -> float:
        total = 0.0
        for ts in state.get("approval_timestamps", []):
            age = max(0.0, now - float(ts))
            weight = 0.5 ** (age / _DECAY_HALF_LIFE_SECONDS)
            total += weight
        return total

    async def get_level(self, action_type: str) -> int:
        """Get current consent level for an action type."""
        await self._ensure_loaded()
        async with self._get_lock():
            state = self._get_state(action_type)
            return int(state["level"])

    async def get_level_name(self, action_type: str) -> str:
        """Get human-readable consent level name."""
        level = await self.get_level(action_type)
        return _LEVEL_NAMES.get(level, "unknown")

    async def get_all_states(self) -> dict[str, dict[str, Any]]:
        """Get all action consent states for display."""
        await self._ensure_loaded()
        # F24: deep-copy under the lock so the caller cannot observe a
        # mid-mutation snapshot if a writer is also in flight.
        async with self._get_lock():
            return copy.deepcopy(self._action_states)

    async def reset(self, action_type: str | None = None) -> None:
        """Reset consent state for one or all action types."""
        # F24: gate the reset behind the same lock readers use, so a
        # plan being constructed cannot bake a now-rescinded level into
        # an outgoing intervention. Lock is released even on exception
        # because ``async with`` always runs ``__aexit__``.
        async with self._get_lock():
            if action_type:
                self._action_states.pop(action_type, None)
            else:
                self._action_states.clear()
            await self._persist()
