"""
Intervention Engine — Trigger Evaluation (DEPRECATED)

.. deprecated::
    This module is superseded by ``cortex.services.state_engine.trigger_policy``
    which is the implementation used by the runtime daemon. This module is kept
    only for backward compatibility with existing tests. New code should use
    ``TriggerPolicy`` from ``cortex.services.state_engine.trigger_policy``.

Evaluates whether an intervention should fire and selects the
appropriate level based on state confidence, complexity, signal quality,
cooldown, and dismissal history.

Levels:
- overlay_only:          confidence > 0.70
- simplified_workspace:  confidence > 0.85
- guided_mode:           confidence > 0.95
"""

from __future__ import annotations

import time
import warnings
from dataclasses import dataclass, field

from cortex.libs.schemas.state import StateEstimate


@dataclass
class DismissalEntry:
    """Tracks a single dismissal event."""

    timestamp: float
    threshold_bump: float = 0.05


@dataclass
class TriggerDecision:
    """Result of trigger evaluation."""

    should_trigger: bool
    level: str | None = None
    reasons: list[str] = field(default_factory=list)
    cooldown_remaining: float = 0.0
    quiet_mode_active: bool = False


class InterventionTrigger:
    """
    Evaluates whether an intervention should fire.

    .. deprecated::
        Use ``cortex.services.state_engine.trigger_policy.TriggerPolicy`` instead.
        This class is kept for backward compatibility only.

    Uses state engine output + workspace complexity to decide. Manages
    cooldown, dismissal history, adaptive thresholds, and quiet mode.
    """

    def __init__(
        self,
        *,
        overlay_threshold: float = 0.70,
        simplified_threshold: float = 0.85,
        guided_threshold: float = 0.95,
        complexity_threshold: float = 0.6,
        cooldown_seconds: float = 60.0,
        dwell_seconds: float = 8.0,
        max_dismissals: int = 3,
        dismissal_window_seconds: float = 300.0,  # 5 min
        quiet_mode_seconds: float = 1800.0,  # 30 min
        dismissal_bump: float = 0.05,
        dismissal_decay_seconds: float = 3600.0,  # 1 hour
    ) -> None:
        warnings.warn(
            "InterventionTrigger is deprecated; use "
            "cortex.services.state_engine.trigger_policy.TriggerPolicy instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        self._overlay_threshold = overlay_threshold
        self._simplified_threshold = simplified_threshold
        self._guided_threshold = guided_threshold
        self._complexity_threshold = complexity_threshold
        self._cooldown_seconds = cooldown_seconds
        self._dwell_seconds = dwell_seconds
        self._max_dismissals = max_dismissals
        self._dismissal_window = dismissal_window_seconds
        self._quiet_mode_seconds = quiet_mode_seconds
        self._dismissal_bump = dismissal_bump
        self._dismissal_decay = dismissal_decay_seconds

        self._last_trigger_time: float | None = None
        self._dismissals: list[DismissalEntry] = []
        self._quiet_mode_until: float = 0.0

    def evaluate(
        self,
        estimate: StateEstimate,
        complexity_score: float = 0.0,
        *,
        current_time: float | None = None,
    ) -> TriggerDecision:
        """
        Evaluate whether to trigger an intervention.

        Args:
            estimate: Current state estimate from the state engine.
            complexity_score: Current workspace complexity (0.0-1.0).
            current_time: Override time for testing.

        Returns:
            TriggerDecision with should_trigger, level, and reasons.
        """
        now = current_time if current_time is not None else time.monotonic()
        reasons: list[str] = []

        # Check quiet mode
        if now < self._quiet_mode_until:
            return TriggerDecision(
                should_trigger=False,
                reasons=["quiet mode active"],
                quiet_mode_active=True,
            )

        # Must be HYPER state
        if estimate.state != "HYPER":
            return TriggerDecision(
                should_trigger=False,
                reasons=[f"state is {estimate.state}, not HYPER"],
            )

        # Get adaptive threshold (raised by dismissals)
        threshold_bump = self._active_threshold_bump(now)
        effective_threshold = self._overlay_threshold + threshold_bump

        # Check confidence vs effective threshold
        if estimate.confidence < effective_threshold:
            return TriggerDecision(
                should_trigger=False,
                reasons=[
                    f"confidence {estimate.confidence:.2f} < threshold {effective_threshold:.2f}"
                ],
            )

        # Check signal quality
        if not estimate.signal_quality.acceptable:
            return TriggerDecision(
                should_trigger=False,
                reasons=["signal quality too low"],
            )

        # Check dwell time
        if estimate.dwell_seconds < self._dwell_seconds:
            return TriggerDecision(
                should_trigger=False,
                reasons=[
                    f"dwell {estimate.dwell_seconds:.1f}s < required {self._dwell_seconds:.1f}s"
                ],
            )

        # Check cooldown
        if self._last_trigger_time is not None:
            elapsed = now - self._last_trigger_time
            if elapsed < self._cooldown_seconds:
                remaining = self._cooldown_seconds - elapsed
                return TriggerDecision(
                    should_trigger=False,
                    reasons=[f"cooldown active ({remaining:.0f}s remaining)"],
                    cooldown_remaining=remaining,
                )

        # All checks passed — select level
        level = self._select_level(estimate.confidence, threshold_bump)
        reasons.append(f"HYPER with confidence {estimate.confidence:.2f}")
        reasons.append(f"dwell {estimate.dwell_seconds:.1f}s")

        if complexity_score >= self._complexity_threshold:
            reasons.append(f"high complexity ({complexity_score:.2f})")

        self._last_trigger_time = now

        return TriggerDecision(
            should_trigger=True,
            level=level,
            reasons=reasons,
        )

    def record_dismissal(self, *, timestamp: float | None = None) -> None:
        """Record that the user dismissed an intervention."""
        now = timestamp if timestamp is not None else time.monotonic()
        self._dismissals.append(
            DismissalEntry(timestamp=now, threshold_bump=self._dismissal_bump)
        )

        # Check for quiet mode activation
        recent = [
            d for d in self._dismissals
            if (now - d.timestamp) < self._dismissal_window
        ]
        if len(recent) >= self._max_dismissals:
            self._quiet_mode_until = now + self._quiet_mode_seconds

    def reset_cooldown(self) -> None:
        """Reset the cooldown timer (e.g., after a long break)."""
        self._last_trigger_time = None

    @property
    def in_quiet_mode(self) -> bool:
        """Check if quiet mode is currently active (using real time)."""
        return time.monotonic() < self._quiet_mode_until

    def is_quiet_mode_at(self, t: float) -> bool:
        """Check if quiet mode is active at a given timestamp."""
        return t < self._quiet_mode_until

    def _select_level(self, confidence: float, threshold_bump: float) -> str:
        """Select intervention level based on confidence."""
        guided = self._guided_threshold + threshold_bump
        simplified = self._simplified_threshold + threshold_bump

        if confidence >= guided:
            return "guided_mode"
        if confidence >= simplified:
            return "simplified_workspace"
        return "overlay_only"

    def _active_threshold_bump(self, now: float) -> float:
        """Calculate the active threshold bump from recent dismissals."""
        total = 0.0
        for d in self._dismissals:
            age = now - d.timestamp
            if age < self._dismissal_decay:
                total += d.threshold_bump
        return total
