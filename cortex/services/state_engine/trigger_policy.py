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

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from cortex.libs.config.settings import InterventionConfig, StateConfig
from cortex.libs.schemas.state import StateEstimate
from cortex.libs.utils.atomic_write import atomic_write_json
from cortex.libs.utils.platform import get_config_dir

logger = logging.getLogger(__name__)


# F21: persisted dismissal-model record version. Bump if the weights
# tuple shape changes so installed clients cold-start instead of loading
# an incompatible shape.
DISMISSAL_MODEL_VERSION: int = 1

# F21: debounce window for disk flushes. Whichever comes first
# (N updates OR T seconds) triggers a write. Tuned to avoid a flush
# storm during burst-dismissal sessions while still bounding loss
# at restart.
_DISMISSAL_FLUSH_EVERY_N_UPDATES: int = 10
_DISMISSAL_FLUSH_EVERY_SECONDS: float = 30.0


def _default_dismissal_model_path() -> Path:
    """Where the persisted dismissal-model record lives (F21).

    Lazy so tests can override via the ``dismissal_model_path``
    constructor kwarg instead of monkeypatching globals.
    """
    return get_config_dir() / "dismissal_model.json"


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
    receptivity_blocked: bool = False
    dismissal_probability: float | None = None


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

    def __init__(
        self,
        config: InterventionConfig | None = None,
        state_config: StateConfig | None = None,
        *,
        hyper_dwell_seconds: float | None = None,
        dismissal_model_path: Path | None = None,
    ) -> None:
        self._config = config or InterventionConfig()
        # Single source of truth for HYPER dwell is StateConfig (v0.2.0 C.5).
        # Tests may inject ``hyper_dwell_seconds`` directly for short cycles.
        if hyper_dwell_seconds is not None:
            self._hyper_dwell_seconds = float(hyper_dwell_seconds)
        else:
            self._hyper_dwell_seconds = float(
                (state_config or StateConfig()).hyper_dwell_seconds
            )

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
        self._dismissals_total: int = 0
        self._approvals_total: int = 0
        self._dismissal_model_weights: tuple[float, float, float] = (0.0, 0.0, 0.0)
        self._dismissal_outcomes: int = 0

        # F21: dismissal-model persistence.
        # Lock guards the (weights, write-debounce counters) tuple so a
        # concurrent ``record_outcome`` cannot tear the persisted record.
        # The on-disk write itself is atomic via ``atomic_write_json``;
        # the lock only protects the in-memory state we are about to
        # serialise.
        self._dismissal_model_path: Path = (
            dismissal_model_path
            if dismissal_model_path is not None
            else _default_dismissal_model_path()
        )
        self._dismissal_persist_lock: threading.Lock = threading.Lock()
        self._dismissal_updates_since_flush: int = 0
        self._dismissal_last_flush_at: float = time.monotonic()
        self._load_dismissal_model()

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
        mic_active: bool = False,
        fullscreen_active: bool = False,
        typing_burst_seconds: float = 0.0,
        within_work_hours: bool = True,
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

        # Compute effective threshold (base + dismissal bumps + adaptive feedback).
        effective_threshold = self._compute_effective_threshold(now)
        confidence = estimate.confidence
        cooldown_remaining = max(
            0.0, self._last_intervention_time + self._config.cooldown_seconds - now
        )
        quiet_active = now < self._quiet_mode_until

        # Receptivity gate (don't interrupt high-friction moments).
        if self._config.receptivity_enforced:
            if self._config.receptivity_block_if_mic_active and mic_active:
                return TriggerDecision(
                    should_trigger=False,
                    reason="Receptivity gate: microphone/call active",
                    confidence=confidence,
                    cooldown_remaining=cooldown_remaining,
                    quiet_mode_active=quiet_active,
                    effective_threshold=effective_threshold,
                    context_complexity=context_complexity,
                    receptivity_blocked=True,
                )
            if self._config.receptivity_block_fullscreen and fullscreen_active:
                return TriggerDecision(
                    should_trigger=False,
                    reason="Receptivity gate: fullscreen active",
                    confidence=confidence,
                    cooldown_remaining=cooldown_remaining,
                    quiet_mode_active=quiet_active,
                    effective_threshold=effective_threshold,
                    context_complexity=context_complexity,
                    receptivity_blocked=True,
                )
            if typing_burst_seconds >= self._config.receptivity_typing_burst_seconds:
                return TriggerDecision(
                    should_trigger=False,
                    reason="Receptivity gate: active typing burst",
                    confidence=confidence,
                    cooldown_remaining=cooldown_remaining,
                    quiet_mode_active=quiet_active,
                    effective_threshold=effective_threshold,
                    context_complexity=context_complexity,
                    receptivity_blocked=True,
                )
            if not within_work_hours:
                return TriggerDecision(
                    should_trigger=False,
                    reason="Receptivity gate: outside configured work hours",
                    confidence=confidence,
                    cooldown_remaining=cooldown_remaining,
                    quiet_mode_active=quiet_active,
                    effective_threshold=effective_threshold,
                    context_complexity=context_complexity,
                    receptivity_blocked=True,
                )

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
        dwell_required = self._hyper_dwell_seconds
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

        dismiss_prob = self._predict_dismiss_probability(
            confidence=confidence,
            context_complexity=context_complexity or 0.0,
            typing_burst_seconds=typing_burst_seconds,
        )
        if (
            self._config.dismissal_model_enabled
            and self._dismissal_outcomes >= 10
            and dismiss_prob > self._config.dismissal_model_threshold
        ):
            return TriggerDecision(
                should_trigger=False,
                reason=f"Predicted dismissal probability too high ({dismiss_prob:.2f})",
                confidence=confidence,
                cooldown_remaining=0.0,
                quiet_mode_active=False,
                effective_threshold=effective_threshold,
                context_complexity=context_complexity,
                dismissal_probability=dismiss_prob,
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
            dismissal_probability=dismiss_prob,
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
        self._dismissals_total += 1

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

    def record_outcome(
        self,
        *,
        dismissed: bool,
        confidence: float = 0.0,
        context_complexity: float = 0.0,
        typing_burst_seconds: float = 0.0,
    ) -> None:
        """Update adaptive thresholding and dismissal model with user feedback."""
        if dismissed:
            self._dismissals_total += 1
        else:
            self._approvals_total += 1
        self._dismissal_outcomes += 1

        # Online logistic update (very small-step SGD).
        y = 1.0 if dismissed else 0.0
        x = np.array(
            [float(confidence), float(context_complexity), min(1.0, float(typing_burst_seconds) / 10.0)],
            dtype=np.float64,
        )
        snapshot: tuple[float, float, float] | None = None
        outcomes_snapshot = 0
        with self._dismissal_persist_lock:
            w = np.array(self._dismissal_model_weights, dtype=np.float64)
            z = float(w @ x)
            p = 1.0 / (1.0 + np.exp(-np.clip(z, -20.0, 20.0)))
            grad = (p - y) * x
            w = w - 0.05 * grad
            self._dismissal_model_weights = (float(w[0]), float(w[1]), float(w[2]))
            self._dismissal_updates_since_flush += 1
            now = time.monotonic()
            should_flush = (
                self._dismissal_updates_since_flush
                >= _DISMISSAL_FLUSH_EVERY_N_UPDATES
                or (now - self._dismissal_last_flush_at)
                >= _DISMISSAL_FLUSH_EVERY_SECONDS
            )
            if should_flush:
                self._dismissal_updates_since_flush = 0
                self._dismissal_last_flush_at = now
                snapshot = self._dismissal_model_weights
                outcomes_snapshot = self._dismissal_outcomes

        if snapshot is not None:
            self._persist_dismissal_model(snapshot, outcomes_snapshot)

    def flush_dismissal_model(self) -> None:
        """Force a persistence write of the current dismissal-model record.

        Useful at shutdown, or in tests that want to observe the latest
        weights without waiting for the debounce window to close (F21).
        """
        with self._dismissal_persist_lock:
            self._dismissal_updates_since_flush = 0
            self._dismissal_last_flush_at = time.monotonic()
            snapshot = self._dismissal_model_weights
            outcomes_snapshot = self._dismissal_outcomes
        self._persist_dismissal_model(snapshot, outcomes_snapshot)

    def _persist_dismissal_model(
        self,
        weights: tuple[float, float, float],
        outcomes: int,
    ) -> None:
        """Write the dismissal-model record to disk atomically (F21)."""
        record = {
            "model_version": DISMISSAL_MODEL_VERSION,
            "weights": [float(w) for w in weights],
            "outcomes": int(outcomes),
            "saved_at": time.time(),
        }
        try:
            atomic_write_json(self._dismissal_model_path, record)
        except OSError as exc:
            logger.warning(
                "Failed to persist dismissal model to %s: %s",
                self._dismissal_model_path,
                exc,
            )

    def _load_dismissal_model(self) -> None:
        """Rehydrate persisted weights on construction (F21).

        Missing file or version mismatch -> cold-start with zeros.
        Malformed JSON is tolerated identically: log and cold-start.
        """
        path = self._dismissal_model_path
        if not path.exists():
            logger.debug("No dismissal model at %s; cold-starting.", path)
            return
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError) as exc:
            logger.warning(
                "Could not read dismissal model at %s (%s); cold-starting.",
                path,
                exc,
            )
            return
        if not isinstance(data, dict):
            logger.warning(
                "Dismissal model at %s is not a JSON object; cold-starting.",
                path,
            )
            return
        version = data.get("model_version")
        if version != DISMISSAL_MODEL_VERSION:
            logger.info(
                "Dismissal model version mismatch (have=%r, want=%r); cold-starting.",
                version,
                DISMISSAL_MODEL_VERSION,
            )
            return
        weights = data.get("weights")
        if (
            not isinstance(weights, list)
            or len(weights) != 3
            or not all(isinstance(w, (int, float)) for w in weights)
        ):
            logger.warning(
                "Dismissal model weights shape invalid at %s; cold-starting.",
                path,
            )
            return
        self._dismissal_model_weights = (
            float(weights[0]),
            float(weights[1]),
            float(weights[2]),
        )
        outcomes = data.get("outcomes", 0)
        if isinstance(outcomes, int) and outcomes >= 0:
            self._dismissal_outcomes = outcomes
        logger.info(
            "Rehydrated dismissal model from %s (outcomes=%d)",
            path,
            self._dismissal_outcomes,
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

        threshold = base + total_bump
        if self._config.adaptive_threshold_enabled:
            feedback_offset = (self._dismissals_total - self._approvals_total) * 0.01
            threshold += float(np.clip(feedback_offset, -0.10, 0.10))
            threshold = float(np.clip(
                threshold,
                self._config.adaptive_threshold_min,
                self._config.adaptive_threshold_max,
            ))
        # Cap at reasonable maximum
        return min(0.99, threshold)

    def _predict_dismiss_probability(
        self,
        *,
        confidence: float,
        context_complexity: float,
        typing_burst_seconds: float,
    ) -> float:
        """Predict dismissal probability from lightweight online model."""
        w = np.array(self._dismissal_model_weights, dtype=np.float64)
        if not np.any(np.abs(w) > 1e-6):
            # Cold start: neutral prior until we have enough labeled outcomes.
            return 0.5
        x = np.array(
            [float(confidence), float(context_complexity), min(1.0, float(typing_burst_seconds) / 10.0)],
            dtype=np.float64,
        )
        z = float(w @ x)
        return float(1.0 / (1.0 + np.exp(-np.clip(z, -20.0, 20.0))))

    def reset(self) -> None:
        """Reset all trigger policy state."""
        self._last_intervention_time = 0.0
        self._dismissals.clear()
        self._threshold_bumps.clear()
        self._quiet_mode_until = 0.0
        self._quiet_mode_count = 0
        self._quiet_mode_count_reset_at = 0.0
        self._intervention_count = 0
        self._dismissal_outcomes = 0
        self._dismissals_total = 0
        self._approvals_total = 0
        self._dismissal_model_weights = (0.0, 0.0, 0.0)
        # F21: also clear the persisted record so a subsequent restart
        # does not re-hydrate the model we just wiped.
        try:
            if self._dismissal_model_path.exists():
                self._dismissal_model_path.unlink()
        except OSError:
            logger.debug(
                "Could not remove dismissal model file at %s",
                self._dismissal_model_path,
            )
