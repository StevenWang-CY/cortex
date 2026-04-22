"""Unbiasedness checks for IPS off-policy value estimates."""

from __future__ import annotations

import numpy as np


def _sigmoid(x: np.ndarray | float) -> np.ndarray | float:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -20.0, 20.0)))


def test_ips_recovers_known_policy_value_within_mc_error():
    rng = np.random.default_rng(123)
    n = 25000

    # Context and known reward model.
    x = rng.normal(0.0, 1.0, size=n)
    p_arm0 = _sigmoid(0.4 + 0.3 * x)
    p_arm1 = _sigmoid(-0.2 + 0.2 * x)

    # Logged propensities (stochastic behavior policy).
    pi0 = _sigmoid(0.15 * x)
    a = rng.binomial(1, pi0, size=n)  # 1 -> arm0, 0 -> arm1

    rewards = np.where(
        a == 1,
        rng.binomial(1, p_arm0, size=n),
        rng.binomial(1, p_arm1, size=n),
    ).astype(np.float64)

    # Target policy: always choose arm0.
    true_value = float(np.mean(p_arm0))

    # IPS estimator V(pi_target) with indicator for chosen arm0.
    ips = np.mean(rewards * (a == 1) / np.clip(pi0, 1e-6, 1.0))

    # Monte-Carlo tolerance (empirical) for n=25k.
    assert abs(float(ips) - true_value) < 0.03
