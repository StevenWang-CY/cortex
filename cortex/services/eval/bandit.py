"""
Eval — Contextual Bandit (LinUCB)

Implements a contextual bandit that learns which intervention types
work best for this specific user given the workspace context.

State features: [state_code, complexity, tab_count, error_count,
                 time_of_day, thrashing_score, stress_integral, consent_level]

Arms: intervention types (overlay_only, simplified_workspace, guided_mode,
      breathing, active_recall, circuit_breaker, none)

Algorithm: LinUCB (Disjoint) — O(d^2) per update, trivially fast on CPU.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# Arm definitions
ARM_LABELS = [
    "overlay_only",
    "simplified_workspace",
    "guided_mode",
    "breathing",
    "active_recall",
    "circuit_breaker",
    "none",
]
N_ARMS = len(ARM_LABELS)
N_FEATURES = 8  # Dimension of context features

# State encoding for feature vector
STATE_CODES = {
    "FLOW": 0.0,
    "HYPO": 0.25,
    "RECOVERY": 0.5,
    "HYPER": 1.0,
    "HYPO_APNEA": 0.3,
}


def encode_context(
    state: str,
    complexity: float = 0.0,
    tab_count: int = 0,
    error_count: int = 0,
    hour: int | None = None,
    thrashing_score: float = 0.0,
    stress_integral: float = 0.0,
    consent_level: int = 1,
) -> np.ndarray:
    """
    Encode workspace context into a feature vector for the bandit.

    All features are normalized to [0, 1] range.
    """
    from datetime import datetime

    if hour is None:
        hour = datetime.now().hour

    return np.array([
        STATE_CODES.get(state, 0.5),
        min(1.0, complexity),
        min(1.0, tab_count / 20.0),
        min(1.0, error_count / 10.0),
        hour / 24.0,
        min(1.0, thrashing_score),
        min(1.0, stress_integral / 1000.0),
        consent_level / 4.0,
    ], dtype=np.float64)


class ContextualBandit:
    """
    LinUCB contextual bandit for intervention type selection.

    Learns which intervention type leads to the highest helpfulness
    reward given the current workspace context.

    Usage:
        bandit = ContextualBandit()
        arm_idx = bandit.select_arm(context_features)
        intervention_type = ARM_LABELS[arm_idx]
        # ... apply intervention, observe reward ...
        bandit.update(context_features, arm_idx, reward)
    """

    def __init__(
        self,
        n_arms: int = N_ARMS,
        n_features: int = N_FEATURES,
        alpha: float = 1.0,
        store: Any = None,
    ) -> None:
        self._n_arms = n_arms
        self._n_features = n_features
        self._alpha = alpha
        self._store = store

        # LinUCB parameters: A = d×d identity, b = d×1 zero
        # Regularization scalar 5.0 (not 1.0) to reduce cold-start variance
        self._A = [np.eye(n_features) * 5.0 for _ in range(n_arms)]
        self._b = [np.zeros(n_features) for _ in range(n_arms)]
        self._total_updates = 0
        self._loaded = False

    async def _ensure_loaded(self) -> None:
        """Load weights from store if available."""
        if self._loaded:
            return
        self._loaded = True
        if self._store is None:
            return
        try:
            data = await self._store.get_json("bandit_weights")
            if data:
                self._from_dict(data)
                logger.info("Loaded bandit weights (%d updates)", self._total_updates)
        except Exception:
            logger.debug("No stored bandit weights, using initial")

    async def _persist(self) -> None:
        """Save weights to store."""
        if self._store is None:
            return
        try:
            data = self._to_dict()
            await self._store.set_json("bandit_weights", data)
        except Exception:
            logger.debug("Failed to persist bandit weights")

    def select_arm(self, context: np.ndarray) -> int:
        """
        Select the best arm (intervention type) for the given context.

        Uses the UCB (Upper Confidence Bound) to balance exploration
        and exploitation.

        Args:
            context: Feature vector of shape (n_features,).

        Returns:
            Index of the selected arm.
        """
        x = context.reshape(-1, 1)  # Column vector
        ucb_values = np.zeros(self._n_arms)

        for a in range(self._n_arms):
            A_inv = np.linalg.inv(self._A[a])
            theta = A_inv @ self._b[a]

            # UCB = theta^T x + alpha * sqrt(x^T A^-1 x)
            exploitation = float(theta @ context)
            exploration = self._alpha * float(np.sqrt(context @ A_inv @ context))
            ucb_values[a] = exploitation + exploration

        selected = int(np.argmax(ucb_values))
        return selected

    def update(
        self,
        context: np.ndarray,
        arm_idx: int,
        reward: float,
    ) -> None:
        """
        Update the bandit with an observed reward.

        Args:
            context: Feature vector used when selecting the arm.
            arm_idx: Index of the arm that was played.
            reward: Observed reward in [-1, 1].
        """
        if arm_idx < 0 or arm_idx >= self._n_arms:
            return

        x = context.reshape(-1, 1)

        # A_a = A_a + x x^T
        self._A[arm_idx] += x @ x.T

        # b_a = b_a + reward * x
        self._b[arm_idx] += reward * context

        self._total_updates += 1

    async def select_arm_async(self, context: np.ndarray) -> int:
        """Async version that ensures weights are loaded."""
        await self._ensure_loaded()
        return self.select_arm(context)

    async def update_async(
        self,
        context: np.ndarray,
        arm_idx: int,
        reward: float,
    ) -> None:
        """Async version that persists after update."""
        self.update(context, arm_idx, reward)
        # Persist every 10 updates
        if self._total_updates % 10 == 0:
            await self._persist()

    def get_arm_label(self, arm_idx: int) -> str:
        """Get human-readable label for an arm index."""
        if 0 <= arm_idx < len(ARM_LABELS):
            return ARM_LABELS[arm_idx]
        return "unknown"

    def get_arm_index(self, arm_label: str) -> int | None:
        """Get arm index by label, or None when unknown."""
        try:
            idx = ARM_LABELS.index(arm_label)
        except ValueError:
            return None
        if idx >= self._n_arms:
            return None
        return idx

    def get_arm_stats(self) -> list[dict]:
        """Get statistics for each arm."""
        stats = []
        for a in range(self._n_arms):
            A_inv = np.linalg.inv(self._A[a])
            theta = A_inv @ self._b[a]
            stats.append({
                "arm": ARM_LABELS[a] if a < len(ARM_LABELS) else f"arm_{a}",
                "mean_theta": float(np.mean(theta)),
                "theta_norm": float(np.linalg.norm(theta)),
            })
        return stats

    def _to_dict(self) -> dict:
        """Serialize for storage."""
        return {
            "n_arms": self._n_arms,
            "n_features": self._n_features,
            "alpha": self._alpha,
            "total_updates": self._total_updates,
            "A": [a.tolist() for a in self._A],
            "b": [b.tolist() for b in self._b],
            "arm_labels": ARM_LABELS[:self._n_arms],
        }

    def _from_dict(self, data: dict) -> None:
        """Restore from serialized state."""
        n_arms = data.get("n_arms", N_ARMS)
        n_features = data.get("n_features", N_FEATURES)

        if n_arms != self._n_arms or n_features != self._n_features:
            logger.warning("Bandit dimensions mismatch, reinitializing")
            return

        self._alpha = data.get("alpha", 1.0)
        self._total_updates = data.get("total_updates", 0)

        A_data = data.get("A", [])
        b_data = data.get("b", [])

        if len(A_data) == n_arms and len(b_data) == n_arms:
            self._A = [np.array(a) for a in A_data]
            self._b = [np.array(b) for b in b_data]
