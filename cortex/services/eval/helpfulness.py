"""
Eval — Helpfulness Tracker

Tracks pre- and post-intervention state to compute a helpfulness
reward signal. Records both implicit signals (undo, ignore) and
explicit signals (thumbs up/down).

The reward signal feeds the contextual bandit learning loop.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# Reward computation weights
_RECOVERY_WEIGHT = 0.40   # Did user return to FLOW?
_COMPLEXITY_WEIGHT = 0.15  # Did complexity decrease?
_RATING_WEIGHT = 0.30     # Explicit user rating
_IMPLICIT_WEIGHT = 0.15   # Was it engaged with vs ignored?

# Timing
_POST_OBSERVATION_SECONDS = 300.0  # 5 minutes post-intervention


class _TrackedIntervention:
    """Internal tracking state for a single intervention."""

    def __init__(
        self,
        intervention_id: str,
        intervention_type: str,
        pre_state: str,
        pre_confidence: float,
        pre_complexity: float,
        pre_tab_count: int,
        pre_error_count: int,
        pre_thrashing: float,
        pre_stress: float,
        started_at: float,
    ) -> None:
        self.intervention_id = intervention_id
        self.intervention_type = intervention_type
        self.pre_state = pre_state
        self.pre_confidence = pre_confidence
        self.pre_complexity = pre_complexity
        self.pre_tab_count = pre_tab_count
        self.pre_error_count = pre_error_count
        self.pre_thrashing = pre_thrashing
        self.pre_stress = pre_stress
        self.started_at = started_at

        # Post-intervention (filled later)
        self.post_state: str | None = None
        self.post_confidence: float = 0.0
        self.post_complexity: float = 0.0
        self.post_tab_count: int = 0
        self.post_error_count: int = 0
        self.ended_at: float | None = None
        self.time_to_flow: float | None = None

        # User signals
        self.was_undone: bool = False
        self.was_ignored: bool = False  # Dismissed in < 2s
        self.was_engaged: bool = False
        self.user_rating: str | None = None  # "thumbs_up" or "thumbs_down"
        self.user_action: str = "dismissed"


class HelpfulnessTracker:
    """
    Tracks intervention helpfulness for the learning loop.

    On intervention start: captures pre-state snapshot.
    On intervention end (or +5 min): captures post-state, computes reward.

    Usage:
        tracker = HelpfulnessTracker(store=redis_store)
        tracker.start_tracking(intervention_id, type, state, context)
        # ... later ...
        record = await tracker.end_tracking(intervention_id, state, context, outcome)
    """

    def __init__(self, store: Any = None) -> None:
        self._store = store
        self._active: dict[str, _TrackedIntervention] = {}
        self._recent_rewards: list[float] = []

    def start_tracking(
        self,
        intervention_id: str,
        intervention_type: str,
        state: str,
        confidence: float,
        complexity: float = 0.0,
        tab_count: int = 0,
        error_count: int = 0,
        thrashing_score: float = 0.0,
        stress_integral: float = 0.0,
    ) -> None:
        """
        Start tracking an intervention.

        Called when an intervention is triggered.
        """
        self._active[intervention_id] = _TrackedIntervention(
            intervention_id=intervention_id,
            intervention_type=intervention_type,
            pre_state=state,
            pre_confidence=confidence,
            pre_complexity=complexity,
            pre_tab_count=tab_count,
            pre_error_count=error_count,
            pre_thrashing=thrashing_score,
            pre_stress=stress_integral,
            started_at=time.monotonic(),
        )

    def record_user_action(
        self,
        intervention_id: str,
        action: str,
        timestamp: float | None = None,
    ) -> None:
        """Record a user action on an intervention."""
        tracked = self._active.get(intervention_id)
        if tracked is None:
            return

        tracked.user_action = action
        ts = timestamp or time.monotonic()

        if action == "engaged":
            tracked.was_engaged = True
        elif action == "dismissed":
            # Check if ignored (dismissed in < 2s)
            if ts - tracked.started_at < 2.0:
                tracked.was_ignored = True

    def record_undo(self, intervention_id: str) -> None:
        """Record that the user undid an intervention."""
        tracked = self._active.get(intervention_id)
        if tracked:
            tracked.was_undone = True

    def record_rating(self, intervention_id: str, rating: str) -> None:
        """Record explicit user rating (thumbs_up/thumbs_down)."""
        tracked = self._active.get(intervention_id)
        if tracked and rating in ("thumbs_up", "thumbs_down"):
            tracked.user_rating = rating

    async def end_tracking(
        self,
        intervention_id: str,
        state: str,
        confidence: float,
        complexity: float = 0.0,
        tab_count: int = 0,
        error_count: int = 0,
    ) -> dict | None:
        """
        End tracking and compute the helpfulness reward.

        Returns a HelpfulnessRecord dict, or None if not tracked.
        """
        tracked = self._active.pop(intervention_id, None)
        if tracked is None:
            return None

        tracked.post_state = state
        tracked.post_confidence = confidence
        tracked.post_complexity = complexity
        tracked.post_tab_count = tab_count
        tracked.post_error_count = error_count
        tracked.ended_at = time.monotonic()

        # Compute time to FLOW
        if state == "FLOW":
            tracked.time_to_flow = tracked.ended_at - tracked.started_at

        # Compute reward
        reward = self._compute_reward(tracked)
        self._recent_rewards.append(reward)
        if len(self._recent_rewards) > 100:
            self._recent_rewards = self._recent_rewards[-100:]

        record = {
            "intervention_id": intervention_id,
            "intervention_type": tracked.intervention_type,
            "pre_state": tracked.pre_state,
            "post_state": tracked.post_state,
            "pre_complexity": tracked.pre_complexity,
            "post_complexity": tracked.post_complexity,
            "pre_tab_count": tracked.pre_tab_count,
            "post_tab_count": tracked.post_tab_count,
            "pre_error_count": tracked.pre_error_count,
            "post_error_count": tracked.post_error_count,
            "time_to_flow_seconds": tracked.time_to_flow,
            "was_undone": tracked.was_undone,
            "was_ignored": tracked.was_ignored,
            "was_engaged": tracked.was_engaged,
            "user_rating": tracked.user_rating,
            "user_action": tracked.user_action,
            "reward_signal": reward,
            "duration_seconds": (tracked.ended_at - tracked.started_at) if tracked.ended_at else None,
        }

        # Persist to store
        if self._store is not None:
            try:
                key = f"helpfulness:{intervention_id}"
                await self._store.set_json(key, record, ttl_seconds=90 * 86400)
            except Exception:
                logger.debug("Failed to persist helpfulness record")

        logger.info(
            "Helpfulness: %s → reward=%.2f (action=%s, rating=%s, flow=%s)",
            intervention_id, reward, tracked.user_action,
            tracked.user_rating, tracked.time_to_flow,
        )
        return record

    def _compute_reward(self, tracked: _TrackedIntervention) -> float:
        """
        Compute helpfulness reward signal in [-1, 1].

        Components:
        1. Recovery signal: Did user return to FLOW?
        2. Complexity reduction: Did workspace simplify?
        3. Explicit rating: Thumbs up/down (30% weight when present)
        4. Implicit signal: Engaged vs ignored vs undone

        When explicit rating is absent, the 30% rating weight is
        redistributed proportionally among the other three signals.
        """
        # Determine weights based on whether explicit rating is present
        explicit_rating = tracked.user_rating
        if explicit_rating is None:
            w_recovery, w_complexity, w_implicit = (0.57, 0.21, 0.22)
        else:
            w_recovery, w_complexity, w_implicit = (0.40, 0.15, 0.15)

        reward = 0.0

        # 1. Recovery (0 to 1)
        recovery = 0.0
        if tracked.post_state == "FLOW":
            recovery = 1.0
        elif tracked.post_state == "RECOVERY":
            recovery = 0.5
        elif tracked.post_state in ("HYPO", "HYPER"):
            recovery = -0.3
        reward += w_recovery * recovery

        # 2. Complexity reduction (-0.5 to 1)
        complexity_delta = tracked.pre_complexity - tracked.post_complexity
        complexity_signal = min(1.0, max(-0.5, complexity_delta * 2.0))
        reward += w_complexity * complexity_signal

        # 3. Explicit rating (-1 to 1) — only contributes when present
        if explicit_rating == "thumbs_up":
            reward += _RATING_WEIGHT * 1.0
        elif explicit_rating == "thumbs_down":
            reward += _RATING_WEIGHT * -1.0

        # 4. Implicit signals (-1 to 1)
        implicit = 0.0
        if tracked.was_undone:
            implicit = -1.0
        elif tracked.was_ignored:
            implicit = -0.5
        elif tracked.was_engaged:
            implicit = 0.5
        reward += w_implicit * implicit

        return float(max(-1.0, min(1.0, reward)))

    @property
    def mean_reward(self) -> float:
        """Mean reward of recent interventions."""
        if not self._recent_rewards:
            return 0.0
        return sum(self._recent_rewards) / len(self._recent_rewards)

    async def get_summary(self) -> dict:
        """Get helpfulness summary statistics."""
        return {
            "total_tracked": len(self._recent_rewards),
            "mean_reward": self.mean_reward,
            "positive_rate": (
                sum(1 for r in self._recent_rewards if r > 0) / len(self._recent_rewards)
                if self._recent_rewards else 0.0
            ),
        }
