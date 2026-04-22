"""Regret smoke tests for AMIP contextual Thompson sampling."""

from __future__ import annotations

import numpy as np

from cortex.services.eval.amip import ARMS, AMIPPolicy


def _log_log_slope(x: np.ndarray, y: np.ndarray) -> float:
    x_log = np.log(np.clip(x, 1.0, None))
    y_log = np.log(np.clip(y, 1e-8, None))
    slope, _ = np.polyfit(x_log, y_log, deg=1)
    return float(slope)


def test_amip_regret_sublinear_smoke(tmp_path):
    """
    In a stationary linear-reward simulator, AMIP regret should grow
    slower than linear on a log-log fit.
    """
    policy = AMIPPolicy(
        storage_root=str(tmp_path),
        n_features=8,
        tau0=0.8,
        tau_min=0.05,
        epsilon_explore=0.0,
        epsilon_explore_after_500=0.0,
    )
    policy._rng = np.random.default_rng(7)

    rng = np.random.default_rng(11)
    true_theta = {
        arm: rng.normal(0.0, 0.10, size=8) for arm in ARMS
    }
    true_theta["task_decompose"] = np.array([0.5, 0.3, 0.2, 0.0, 0.0, 0.0, 0.0, 0.0])

    t_max = 5000
    cumulative_regret = []
    regret = 0.0

    for _t in range(1, t_max + 1):
        x = rng.normal(0.0, 1.0, size=8)
        expected = {arm: float(true_theta[arm] @ x) for arm in ARMS}
        optimal = max(expected.values())

        decision = policy.choose_action(
            x,
            confidence=0.95,
            receptive=True,
            stress_ratio=0.0,
        )
        chosen_expected = expected[decision.action]

        reward = chosen_expected + float(rng.normal(0.0, 0.05))
        policy.update_reward(decision.decision_id, reward)

        regret += max(0.0, optimal - chosen_expected)
        cumulative_regret.append(regret)

    x_axis = np.arange(200, t_max + 1)
    y_vals = np.array(cumulative_regret[199:], dtype=np.float64)
    slope = _log_log_slope(x_axis, y_vals)

    assert slope < 0.6, f"expected sub-linear regret slope, got {slope:.3f}"
