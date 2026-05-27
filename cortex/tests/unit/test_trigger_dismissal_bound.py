"""P1-4: InterventionTrigger._dismissals is a bounded deque (maxlen=256).

Ensures:
1. Recording 1000 dismissals never grows beyond 256.
2. _active_threshold_bump result for a representative window matches
   the reference (unbounded) calculation within float tolerance.
"""

from __future__ import annotations

import warnings
from collections import deque

import pytest


def _make_trigger(**kwargs):  # type: ignore[no-untyped-def]
    """Return an InterventionTrigger ignoring the deprecation warning."""
    from cortex.services.intervention_engine.trigger import InterventionTrigger

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return InterventionTrigger(**kwargs)


class TestDismissalBound:
    def test_deque_bounded_at_256(self) -> None:
        """Recording more than 256 dismissals keeps the deque at maxlen."""
        trigger = _make_trigger(dismissal_bump=0.01, dismissal_decay_seconds=7200.0)
        now = 1_000_000.0
        for i in range(1000):
            trigger.record_dismissal(timestamp=now + i * 0.1)

        assert isinstance(trigger._dismissals, deque)
        assert len(trigger._dismissals) <= 256

    def test_bump_matches_reference_within_tolerance(self) -> None:
        """Active bump for recent entries matches the unbounded reference sum."""
        trigger = _make_trigger(dismissal_bump=0.05, dismissal_decay_seconds=3600.0)
        base = 1_000_000.0

        # Record 200 recent dismissals (all within the decay window)
        for i in range(200):
            trigger.record_dismissal(timestamp=base + i)

        # Reference: each of the 200 entries contributes 0.05
        expected = 200 * 0.05

        bump = trigger._active_threshold_bump(now=base + 200)
        assert bump == pytest.approx(expected, rel=1e-6)

    def test_expired_entries_pruned_by_bump(self) -> None:
        """Entries older than decay window are pruned during bump calculation."""
        decay = 100.0
        trigger = _make_trigger(dismissal_bump=0.1, dismissal_decay_seconds=decay)
        now = 1_000_000.0

        # 10 old dismissals (outside decay window)
        for i in range(10):
            trigger.record_dismissal(timestamp=now - 200.0 + i)

        # 5 recent dismissals (inside decay window)
        for i in range(5):
            trigger.record_dismissal(timestamp=now - 50.0 + i)

        bump = trigger._active_threshold_bump(now=now)

        # Only the 5 recent ones should count
        assert bump == pytest.approx(5 * 0.1, rel=1e-6)
        # Old entries were pruned
        assert all(d.timestamp >= now - decay for d in trigger._dismissals)
