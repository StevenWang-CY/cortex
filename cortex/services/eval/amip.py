"""
Eval — Adaptive Microrandomized Intervention Policy (AMIP)
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

ARMS = [
    "no_action",
    "workspace_simplify",
    "task_decompose",
    "breath_box",
    "nature_break",
    "flow_shield",
    "defusion_prompt",
    "circuit_breaker",
]

RECOVERY_ARMS = {"breath_box", "nature_break", "circuit_breaker"}


@dataclass
class AMIPDecision:
    decision_id: str
    action: str
    probabilities: dict[str, float]
    features: list[float]
    timestamp: float


class AMIPPolicy:
    """
    Contextual Thompson Sampling with safety-floor constrained exploration.
    """

    def __init__(
        self,
        *,
        storage_root: str,
        n_features: int,
        tau0: float = 1.0,
        tau_min: float = 0.1,
        epsilon_explore: float = 0.05,
        epsilon_explore_after_500: float = 0.01,
        stress_ratio_threshold: float = 1.0,
    ) -> None:
        self._n_features = n_features
        self._tau0 = tau0
        self._tau_min = tau_min
        self._epsilon = epsilon_explore
        self._epsilon_after_500 = epsilon_explore_after_500
        self._stress_ratio_threshold = stress_ratio_threshold
        self._rng = np.random.default_rng()

        self._A = {arm: np.eye(n_features, dtype=np.float64) for arm in ARMS}
        self._b = {arm: np.zeros(n_features, dtype=np.float64) for arm in ARMS}
        self._counts = dict.fromkeys(ARMS, 0)
        self._decisions: dict[str, AMIPDecision] = {}

        root = Path(storage_root)
        self._policy_log_dir = root / "policy_log"
        self._policy_log_dir.mkdir(parents=True, exist_ok=True)

    @property
    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    def choose_action(
        self,
        features: np.ndarray,
        *,
        confidence: float,
        receptive: bool,
        stress_ratio: float,
    ) -> AMIPDecision:
        x = features.astype(np.float64).reshape(-1)
        if x.shape[0] != self._n_features:
            raise ValueError("feature dimension mismatch")

        n_user = max(1, sum(self._counts.values()))
        tau = max(self._tau_min, self._tau0 / math.sqrt(n_user))
        eps = self._epsilon_after_500 if n_user >= 500 else self._epsilon

        scores: dict[str, float] = {}
        for arm in ARMS:
            a_inv = np.linalg.inv(self._A[arm])
            mu = a_inv @ self._b[arm]
            theta_sample = self._rng.multivariate_normal(mean=mu, cov=a_inv)
            scores[arm] = float(theta_sample @ x)

        probs = self._softmax(scores, tau=tau)
        probs = self._apply_safety_floor(
            probs,
            confidence=confidence,
            receptive=receptive,
            stress_ratio=stress_ratio,
            epsilon=eps,
        )
        action = self._sample_from_probs(probs)

        decision_id = f"amip_{int(time.time() * 1000)}_{self._rng.integers(1000, 9999)}"
        decision = AMIPDecision(
            decision_id=decision_id,
            action=action,
            probabilities=probs,
            features=x.tolist(),
            timestamp=time.time(),
        )
        self._decisions[decision_id] = decision
        self._append_wal(
            {
                "decision_id": decision_id,
                "timestamp": decision.timestamp,
                "features": decision.features,
                "probabilities": probs,
                "action": action,
                "confidence": confidence,
                "receptive": receptive,
                "stress_ratio": stress_ratio,
                "tau": tau,
                "epsilon": eps,
            }
        )
        return decision

    def update_reward(self, decision_id: str, reward: float) -> None:
        decision = self._decisions.get(decision_id)
        if decision is None:
            return
        arm = decision.action
        x = np.array(decision.features, dtype=np.float64)
        self._A[arm] += np.outer(x, x)
        self._b[arm] += float(reward) * x
        self._counts[arm] += 1
        self._append_wal(
            {
                "decision_id": decision_id,
                "reward": float(reward),
                "action": arm,
                "updated_at": time.time(),
            }
        )

    def get_posteriors(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for arm in ARMS:
            a_inv = np.linalg.inv(self._A[arm])
            theta = a_inv @ self._b[arm]
            out[arm] = {
                "theta_norm": float(np.linalg.norm(theta)),
                "count": float(self._counts[arm]),
            }
        return out

    def _append_wal(self, record: dict[str, Any]) -> None:
        day = time.strftime("%Y-%m-%d")
        path = self._policy_log_dir / f"{day}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=True) + "\n")

    @staticmethod
    def _softmax(scores: dict[str, float], *, tau: float) -> dict[str, float]:
        keys = list(scores.keys())
        vals = np.array([scores[k] for k in keys], dtype=np.float64)
        vals = vals / max(1e-6, tau)
        vals = vals - np.max(vals)
        exp = np.exp(vals)
        denom = float(np.sum(exp)) if np.sum(exp) > 1e-12 else 1.0
        probs = exp / denom
        return {k: float(p) for k, p in zip(keys, probs, strict=False)}

    def _apply_safety_floor(
        self,
        probs: dict[str, float],
        *,
        confidence: float,
        receptive: bool,
        stress_ratio: float,
        epsilon: float,
    ) -> dict[str, float]:
        p = dict(probs)
        if not receptive or confidence < 0.5:
            return {arm: (1.0 if arm == "no_action" else 0.0) for arm in ARMS}

        if stress_ratio >= self._stress_ratio_threshold:
            p = dict.fromkeys(ARMS, 0.0)
            # Deterministic safety floor: always prioritize active recovery.
            p["no_action"] = 0.0
            p["breath_box"] = 0.45
            p["nature_break"] = 0.30
            p["circuit_breaker"] = 0.25

        # Epsilon floor on feasible arms.
        feasible = [arm for arm, prob in p.items() if prob > 0.0]
        if feasible:
            for arm in feasible:
                p[arm] = max(p[arm], epsilon)
            total = sum(p.values())
            if total > 0:
                p = {k: v / total for k, v in p.items()}
        return p

    def _sample_from_probs(self, probs: dict[str, float]) -> str:
        keys = list(probs.keys())
        vals = np.array([probs[k] for k in keys], dtype=np.float64)
        vals = vals / np.sum(vals)
        idx = int(self._rng.choice(len(keys), p=vals))
        return keys[idx]
