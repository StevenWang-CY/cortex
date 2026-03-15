"""
State Engine — Trigger Policy

Determines when interventions should be triggered based on state
estimates, signal quality, cooldown periods, and dismissal tracking.

Trigger conditions (all must be met):
1. State is HYPER
2. Confidence > 0.85
3. Workspace complexity > 0.7
4. Signal quality acceptable
5. Cooldown period elapsed (60s since last intervention)
6. Not in quiet mode (3 dismissals in 5 min → progressive quiet: 15/30/60 min)
7. Dwell time met (15s in HYPER state — sustained overwhelm, not transient spikes)

Adaptive behavior:
- Each dismissal raises trigger threshold by +0.05 for 1 hour
- 3 dismissals within 5 minutes → progressive quiet mode (15→30→60 min)
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field

from cortex.libs.config.settings import InterventionConfig
from cortex.libs.schemas.state import StateEstimate

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DismissalEvent:
    """Record of a user dismissing an intervention."""

    timestamp: float


@dataclass(frozen=True)
class TriggerDecision:
    """Result of trigger policy evaluation."""

    should_trigger: bool
    reason: str
    confidence: float
    cooldown_remaining: float  # Seconds until cooldown expires
    quiet_mode_active: bool
    effective_threshold: float  # Adjusted threshold after dismissals
    context_complexity: float | None = None


class TriggerPolicy:
    """
    Evaluates whether an intervention should be triggered.

    Tracks cooldowns, dismissals, quiet mode, and adaptive thresholds
    to determine if an intervention is appropriate.

    Usage:
        policy = TriggerPolicy()
        decision = policy.evaluate(state_estimate)
        if decision.should_trigger:
            # ... trigger intervention ...
            pass
        # After user dismisses:
        policy.record_dismissal()
    """

    def __init__(self, config: InterventionConfig | None = None) -> None:
        self._config = config or InterventionConfig()

        # Cooldown tracking
        self._last_intervention_time: float = 0.0

        # Dismissal tracking
        self._dismissals: deque[DismissalEvent] = deque(maxlen=100)
        self._threshold_bumps: deque[tuple[float, float]] = deque(maxlen=50)

        # Quiet mode
        self._quiet_mode_until: float = 0.0
        self._quiet_mode_count: int = 0
        self._quiet_mode_count_reset_at: float = 0.0

        # Intervention counter
        self._intervention_count: int = 0

    @property
    def is_quiet_mode(self) -> bool:
        """Check if quiet mode is currently active."""
        return time.monotonic() < self._quiet_mode_until

    @property
    def intervention_count(self) -> int:
        return self._intervention_count

    def evaluate(
        self,
        estimate: StateEstimate,
        *,
        context_complexity: float | None = None,
        current_time: float | None = None,
    ) -> TriggerDecision:
        """
        Evaluate whether an intervention should be triggered.

        Args:
            estimate: Current state estimate from the smoother.
            current_time: Reference time. Defaults to now.

        Returns:
            TriggerDecision with trigger verdict and explanation.
        """
        now = current_time or time.monotonic()

        # Compute effective threshold (base + dismissal bumps)
        effective_threshold = self._compute_effective_threshold(now)
        confidence = estimate.confidence
        cooldown_remaining = max(
            0.0, self._last_intervention_time + self._config.cooldown_seconds - now
        )
        quiet_active = now < self._quiet_mode_until

        # Check quiet mode
        if quiet_active:
            return TriggerDecision(
                should_trigger=False,
                reason="Quiet mode active",
                confidence=confidence,
                cooldown_remaining=cooldown_remaining,
                quiet_mode_active=True,
                effective_threshold=effective_threshold,
                context_complexity=context_complexity,
            )

        # Check cooldown
        if cooldown_remaining > 0:
            return TriggerDecision(
                should_trigger=False,
                reason=f"Cooldown active ({cooldown_remaining:.0f}s remaining)",
                confidence=confidence,
                cooldown_remaining=cooldown_remaining,
                quiet_mode_active=False,
                effective_threshold=effective_threshold,
                context_complexity=context_complexity,
            )

        # Check state is HYPER
        if not estimate.is_overwhelmed:
            return TriggerDecision(
                should_trigger=False,
                reason=f"State is {estimate.state}, not HYPER",
                confidence=confidence,
                cooldown_remaining=0.0,
                quiet_mode_active=False,
                effective_threshold=effective_threshold,
                context_complexity=context_complexity,
            )

        # Check confidence exceeds effective threshold
        if confidence < effective_threshold:
            return TriggerDecision(
                should_trigger=False,
                reason=f"Confidence {confidence:.2f} below threshold {effective_threshold:.2f}",
                confidence=confidence,
                cooldown_remaining=0.0,
                quiet_mode_active=False,
                effective_threshold=effective_threshold,
                context_complexity=context_complexity,
            )

        if (
            context_complexity is not None
            and context_complexity < self._config.complexity_threshold
        ):
            return TriggerDecision(
                should_trigger=False,
                reason=(
                    f"Workspace complexity {context_complexity:.2f} below "
                    f"{self._config.complexity_threshold:.2f}"
                ),
                confidence=confidence,
                cooldown_remaining=0.0,
                quiet_mode_active=False,
                effective_threshold=effective_threshold,
                context_complexity=context_complexity,
            )

        # Check signal quality — with telemetry-only fallback
        # In poor lighting (dorm rooms at night), webcam signals degrade but
        # behavioral telemetry (mouse, keyboard, tab switching) remains reliable.
        # Allow interventions when telemetry is strong, with stricter confidence.
        if not estimate.signal_quality.acceptable:
            telemetry_fallback = (
                estimate.signal_quality.telemetry >= 0.7
                and confidence >= min(0.95, effective_threshold + 0.10)
            )
            if not telemetry_fallback:
                return TriggerDecision(
                    should_trigger=False,
                    reason=f"Signal quality too low ({estimate.signal_quality.overall:.2f})",
                    confidence=confidence,
                    cooldown_remaining=0.0,
                    quiet_mode_active=False,
                    effective_threshold=effective_threshold,
                    context_complexity=context_complexity,
                )

        # Check dwell time (must be in HYPER for >= hyper_dwell_seconds)
        dwell_required = self._config.hyper_dwell_seconds
        if estimate.dwell_seconds < dwell_required:
            return TriggerDecision(
                should_trigger=False,
                reason=f"Dwell time {estimate.dwell_seconds:.1f}s < {dwell_required:.0f}s required",
                confidence=confidence,
                cooldown_remaining=0.0,
                quiet_mode_active=False,
                effective_threshold=effective_threshold,
                context_complexity=context_complexity,
            )

        # All conditions met — trigger intervention
        return TriggerDecision(
            should_trigger=True,
            reason="All trigger conditions met",
            confidence=confidence,
            cooldown_remaining=0.0,
            quiet_mode_active=False,
            effective_threshold=effective_threshold,
            context_complexity=context_complexity,
        )

    def record_intervention(self, timestamp: float | None = None) -> None:
        """Record that an intervention was triggered."""
        now = timestamp or time.monotonic()
        self._last_intervention_time = now
        self._intervention_count += 1
        logger.info(f"Intervention #{self._intervention_count} triggered")

    def record_dismissal(self, timestamp: float | None = None) -> None:
        """
        Record that the user dismissed an intervention.

        Tracks dismissals for quiet mode and adaptive thresholds.
        """
        now = timestamp or time.monotonic()
        self._dismissals.append(DismissalEvent(timestamp=now))

        # Add threshold bump (+0.05 for 1 hour)
        expiry = now + self._config.dismissal_decay_hours * 3600.0
        self._threshold_bumps.append((self._config.dismissal_threshold_bump, expiry))

        # Check for quiet mode trigger (3 dismissals in 5 min)
        recent_window = self._config.dismissal_window_minutes * 60.0
        recent_dismissals = sum(
            1 for d in self._dismissals
            if d.timestamp >= now - recent_window
        )

        if recent_dismissals >= self._config.max_dismissals:
            # Reset escalation counter if >2 hours since last quiet mode
            if now > self._quiet_mode_count_reset_at:
                self._quiet_mode_count = 0

            self._quiet_mode_count += 1
            # Progressive escalation: 15min → 30min → 60min
            durations = [
                self._config.quiet_mode_minutes,       # 15 min (base)
                self._config.quiet_mode_minutes * 2,    # 30 min
                self._config.quiet_mode_minutes * 4,    # 60 min
            ]
            minutes = durations[min(self._quiet_mode_count - 1, len(durations) - 1)]
            self._quiet_mode_until = now + minutes * 60.0
            self._quiet_mode_count_reset_at = now + 2 * 3600.0  # Reset after 2 hours

            logger.info(
                f"Quiet mode activated for {minutes} minutes (level {self._quiet_mode_count}, "
                f"{recent_dismissals} dismissals in {self._config.dismissal_window_minutes} min)"
            )

    def activate_quiet_mode(
        self,
        *,
        duration_minutes: int | None = None,
        current_time: float | None = None,
    ) -> None:
        """Force quiet mode on for an explicit duration."""
        now = current_time or time.monotonic()
        minutes = duration_minutes or self._config.quiet_mode_minutes
        self._quiet_mode_until = now + max(1, minutes) * 60.0

    def clear_quiet_mode(self) -> None:
        """Disable quiet mode immediately."""
        self._quiet_mode_until = 0.0

    def _compute_effective_threshold(self, now: float) -> float:
        """
        Compute the effective trigger threshold.

        Base threshold (0.85) + active dismissal bumps.
        """
        base = self._config.overlay_threshold

        # Add active threshold bumps
        total_bump = sum(
            bump for bump, expiry in self._threshold_bumps
            if expiry > now
        )

        # Cap at reasonable maximum
        return min(0.99, base + total_bump)

    def reset(self) -> None:
        """Reset all trigger policy state."""
        self._last_intervention_time = 0.0
        self._dismissals.clear()
        self._threshold_bumps.clear()
        self._quiet_mode_until = 0.0
        self._quiet_mode_count = 0
        self._quiet_mode_count_reset_at = 0.0
        self._intervention_count = 0
