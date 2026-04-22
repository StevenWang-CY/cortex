"""Safety-floor invariants for AMIP policy."""

from __future__ import annotations

import numpy as np

from cortex.services.eval.amip import AMIPPolicy


def test_safety_floor_forces_recovery_arms_under_high_stress(tmp_path):
    policy = AMIPPolicy(storage_root=str(tmp_path), n_features=8)
    x = np.ones(8, dtype=np.float64)
    decision = policy.choose_action(
        x,
        confidence=0.92,
        receptive=True,
        stress_ratio=1.2,
    )
    probs = decision.probabilities
    assert probs["no_action"] == 0.0
    assert max(probs["breath_box"], probs["nature_break"], probs["circuit_breaker"]) >= 0.4


def test_safety_floor_blocks_when_not_receptive(tmp_path):
    policy = AMIPPolicy(storage_root=str(tmp_path), n_features=8)
    x = np.ones(8, dtype=np.float64)
    decision = policy.choose_action(
        x,
        confidence=0.9,
        receptive=False,
        stress_ratio=0.5,
    )
    probs = decision.probabilities
    assert probs["no_action"] == 1.0
