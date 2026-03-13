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
        self._action_states: dict[str, dict] = {}
        self._loaded = False

    async def _ensure_loaded(self) -> None:
        """Load consent state from store if available."""
        if self._loaded:
            return
        self._loaded = True
        if self._store is None:
            return
        try:
            data = await self._store.get_json("consent_ladder_state")
            if data and isinstance(data, dict):
                self._action_states = data.get("action_states", {})
                if "global_max" in data:
                    self._policy.global_max_level = data["global_max"]
        except Exception:
            logger.debug("No stored consent state found, using defaults")

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

    def _get_state(self, action_type: str) -> dict:
        """Get or create state for an action type."""
        if action_type not in self._action_states:
            self._action_states[action_type] = {
                "level": self._policy.get_minimum_level(action_type),
                "approvals": 0,
                "rejections": 0,
                "total_approvals": 0,
                "last_approval": None,
                "last_rejection": None,
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
        state = self._get_state(action_type)
        state["approvals"] += 1
        state["total_approvals"] += 1
        state["last_approval"] = time.time()

        # Check for escalation
        if (
            state["approvals"] >= self._escalation_threshold
            and state["level"] < AUTONOMOUS_ACT
        ):
            old_level = state["level"]
            state["level"] = min(state["level"] + 1, AUTONOMOUS_ACT)
            state["approvals"] = 0  # Reset counter for next level
            logger.info(
                "Consent escalated for '%s': %s → %s (after %d approvals)",
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
        state = self._get_state(action_type)
        state["rejections"] += 1
        state["last_rejection"] = time.time()

        # 3 rejections at current level → de-escalate
        if state["rejections"] >= 3 and state["level"] > SUGGEST:
            old_level = state["level"]
            state["level"] = max(state["level"] - 1, SUGGEST)
            state["rejections"] = 0
            state["approvals"] = 0
            logger.info(
                "Consent de-escalated for '%s': %s → %s (after 3 rejections)",
                action_type,
                _LEVEL_NAMES.get(old_level, "?"),
                _LEVEL_NAMES.get(state["level"], "?"),
            )

        await self._persist()

    async def get_level(self, action_type: str) -> int:
        """Get current consent level for an action type."""
        await self._ensure_loaded()
        state = self._get_state(action_type)
        return state["level"]

    async def get_level_name(self, action_type: str) -> str:
        """Get human-readable consent level name."""
        level = await self.get_level(action_type)
        return _LEVEL_NAMES.get(level, "unknown")

    async def get_all_states(self) -> dict[str, dict]:
        """Get all action consent states for display."""
        await self._ensure_loaded()
        return dict(self._action_states)

    async def reset(self, action_type: str | None = None) -> None:
        """Reset consent state for one or all action types."""
        if action_type:
            self._action_states.pop(action_type, None)
        else:
            self._action_states.clear()
        await self._persist()
