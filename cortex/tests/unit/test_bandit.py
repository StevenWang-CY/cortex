"""Tests for the LinUCB Contextual Bandit."""

from __future__ import annotations

import numpy as np
import pytest

from cortex.services.eval.bandit import (
    ARM_LABELS,
    N_ARMS,
    N_FEATURES,
    ContextualBandit,
    encode_context,
)


@pytest.fixture
def bandit():
    return ContextualBandit()


@pytest.fixture
def context():
    return encode_context("HYPER", complexity=0.5, tab_count=10, error_count=2, hour=14)


# ---------------------------------------------------------------------------
# select_arm returns valid arm index (0-6)
# ---------------------------------------------------------------------------

class TestSelectArm:
    def test_returns_valid_index(self, bandit, context):
        arm = bandit.select_arm(context)
        assert 0 <= arm < N_ARMS

    def test_returns_int(self, bandit, context):
        arm = bandit.select_arm(context)
        assert isinstance(arm, int)

    def test_different_contexts_may_differ(self, bandit):
        """Different contexts should potentially yield different arms."""
        ctx1 = encode_context("FLOW", complexity=0.0, tab_count=2, hour=10)
        ctx2 = encode_context("HYPER", complexity=1.0, tab_count=20, hour=22)
        # We cannot guarantee they differ, but both must be valid
        a1 = bandit.select_arm(ctx1)
        a2 = bandit.select_arm(ctx2)
        assert 0 <= a1 < N_ARMS
        assert 0 <= a2 < N_ARMS


# ---------------------------------------------------------------------------
# Update with reward doesn't crash
# ---------------------------------------------------------------------------

class TestUpdate:
    def test_update_does_not_raise(self, bandit, context):
        arm = bandit.select_arm(context)
        bandit.update(context, arm, reward=0.5)

    def test_update_increments_total(self, bandit, context):
        assert bandit._total_updates == 0
        bandit.update(context, 0, 1.0)
        assert bandit._total_updates == 1

    def test_update_invalid_arm_is_noop(self, bandit, context):
        bandit.update(context, -1, 1.0)
        bandit.update(context, 999, 1.0)
        assert bandit._total_updates == 0

    def test_negative_reward(self, bandit, context):
        bandit.update(context, 0, -1.0)
        assert bandit._total_updates == 1


# ---------------------------------------------------------------------------
# Convergence test
# ---------------------------------------------------------------------------

class TestConvergence:
    def test_arm2_dominates_after_training(self):
        """
        After 100 rounds where arm 2 always gets reward 1.0 and others get 0.0,
        arm 2 should be selected most often on subsequent queries.
        """
        bandit = ContextualBandit(alpha=0.5)
        ctx = encode_context("HYPER", complexity=0.5, tab_count=10, hour=14)

        # Train: arm 2 always rewarded, others always zero
        for _ in range(100):
            for arm in range(N_ARMS):
                reward = 1.0 if arm == 2 else 0.0
                bandit.update(ctx, arm, reward)

        # Evaluate: select 50 times and check arm 2 dominates
        selections = [bandit.select_arm(ctx) for _ in range(50)]
        arm2_count = selections.count(2)
        assert arm2_count >= 40, (
            f"Expected arm 2 to be selected at least 40/50 times, got {arm2_count}"
        )


# ---------------------------------------------------------------------------
# Serialization (_to_dict / _from_dict)
# ---------------------------------------------------------------------------

class TestSerialization:
    def test_roundtrip(self, bandit, context):
        """Serialize and deserialize should preserve state."""
        # Train a bit
        bandit.update(context, 0, 1.0)
        bandit.update(context, 3, -0.5)
        bandit.update(context, 2, 0.8)

        data = bandit._to_dict()

        # Restore into a fresh bandit
        restored = ContextualBandit()
        restored._from_dict(data)

        assert restored._total_updates == bandit._total_updates
        assert restored._alpha == bandit._alpha

        # A matrices and b vectors should match
        for i in range(N_ARMS):
            np.testing.assert_array_almost_equal(restored._A[i], bandit._A[i])
            np.testing.assert_array_almost_equal(restored._b[i], bandit._b[i])

    def test_to_dict_structure(self, bandit):
        data = bandit._to_dict()
        assert "n_arms" in data
        assert "n_features" in data
        assert "alpha" in data
        assert "total_updates" in data
        assert "A" in data
        assert "b" in data
        assert len(data["A"]) == N_ARMS
        assert len(data["b"]) == N_ARMS

    def test_dimension_mismatch_skips_restore(self, bandit):
        """If dimensions don't match, _from_dict should not overwrite."""
        data = bandit._to_dict()
        data["n_arms"] = 999  # mismatch

        original_updates = bandit._total_updates
        bandit._from_dict(data)
        assert bandit._total_updates == original_updates


# ---------------------------------------------------------------------------
# encode_context
# ---------------------------------------------------------------------------

class TestEncodeContext:
    def test_output_shape(self):
        ctx = encode_context("FLOW", hour=12)
        assert ctx.shape == (N_FEATURES,)

    def test_values_normalized(self):
        ctx = encode_context(
            "HYPER", complexity=0.8, tab_count=40, error_count=20,
            hour=12, thrashing_score=2.0, stress_integral=5000, consent_level=4,
        )
        # All values should be clipped to [0, 1]
        assert all(0.0 <= v <= 1.0 for v in ctx)

    def test_state_codes(self):
        flow = encode_context("FLOW", hour=0)
        hyper = encode_context("HYPER", hour=0)
        assert flow[0] == 0.0
        assert hyper[0] == 1.0

    def test_arm_labels_count(self):
        assert len(ARM_LABELS) == N_ARMS
