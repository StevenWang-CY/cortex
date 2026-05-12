"""
State Engine — Score Smoother

Applies Exponential Moving Average (EMA) smoothing over score history,
hysteresis thresholds for state transitions, and dwell time enforcement
before confirming state changes.

Produces StateEstimate output every 500ms.

Parameters (from StateConfig):
- EMA alpha: 0.3 (higher = more responsive, less smooth)
- Entry threshold: 0.85 (score must exceed to enter state)
- Exit threshold: 0.70 (score must drop below to exit state)
- Dwell times: HYPER=8s, HYPO=15s, FLOW=15s
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass

import numpy as np

from cortex.libs.config.settings import StateConfig
from cortex.libs.schemas.state import (
    SignalQuality,
    StateEstimate,
    StateScores,
    StateTransition,
    UserState,
)

logger = logging.getLogger(__name__)


@dataclass
class SmoothedScores:
    """EMA-smoothed state scores."""

    flow: float = 0.3  # Start with slight flow assumption
    hypo: float = 0.0
    hyper: float = 0.0
    recovery: float = 0.0

    def to_state_scores(self) -> StateScores:
        return StateScores(
            flow=self.flow, hypo=self.hypo,
            hyper=self.hyper, recovery=self.recovery,
        )

    def dominant(self) -> tuple[UserState, float]:
        scores = {
            UserState.FLOW: self.flow,
            UserState.HYPO: self.hypo,
            UserState.HYPER: self.hyper,
            UserState.RECOVERY: self.recovery,
        }
        state = max(scores, key=lambda k: scores[k])
        return state, scores[state]


class ScoreSmoother:
    """
    Smooths state scores and applies hysteresis for state transitions.

    Maintains:
    - EMA-smoothed scores for all four states
    - Current confirmed state with dwell time
    - State transition history
    - Hysteresis to prevent flickering between states

    Usage:
        smoother = ScoreSmoother()
        estimate = smoother.update(raw_scores, signal_quality, timestamp)
    """

    def __init__(self, config: StateConfig | None = None) -> None:
        self._config = config or StateConfig()
        self._alpha = self._config.ema_alpha
        self._probability_temperature = 1.0

        # Smoothed scores
        self._smoothed = SmoothedScores()

        # Current state
        self._current_state = UserState.FLOW
        self._state_entered_at: float | None = None
        self._dwell_seconds: float = 0.0

        # Candidate state (being evaluated for transition)
        self._candidate_state: UserState | None = None
        self._candidate_since: float = 0.0

        # Transition history
        self._transitions: deque[StateTransition] = deque(maxlen=100)

        # Latest estimate
        self._latest: StateEstimate | None = None

    @property
    def current_state(self) -> UserState:
        return self._current_state

    @property
    def latest_estimate(self) -> StateEstimate | None:
        return self._latest

    @property
    def transitions(self) -> list[StateTransition]:
        return list(self._transitions)

    def update(
        self,
        raw_scores: StateScores,
        signal_quality: SignalQuality,
        timestamp: float | None = None,
        *,
        ml_p_hyper: float | None = None,
        ml_alpha: float = 0.0,
    ) -> StateEstimate:
        """
        Update smoothed scores and produce a StateEstimate.

        Args:
            raw_scores: Raw state scores from the rule scorer.
            signal_quality: Per-channel signal quality.
            timestamp: Current time. Defaults to now.
            ml_p_hyper: Optional probability of HYPER from the per-user
                logistic classifier (C.2). Blended into the smoothed
                hyper score using ``ml_alpha``.
            ml_alpha: Blend weight in ``[0, 1]``. ``0`` keeps the rule
                output; ``1`` replaces it entirely. The daemon ramps this
                with training-data volume (see ``StateConfig.ml_alpha_max``
                and ``ml_alpha_full_at_episodes``).

        Returns:
            StateEstimate with smoothed state, confidence, and reasons.
        """
        now = timestamp if timestamp is not None else time.monotonic()

        # Initialize state_entered_at on first update
        if self._state_entered_at is None:
            self._state_entered_at = now

        # Apply EMA smoothing
        self._smoothed.flow = self._ema(self._smoothed.flow, raw_scores.flow)
        self._smoothed.hypo = self._ema(self._smoothed.hypo, raw_scores.hypo)
        self._smoothed.hyper = self._ema(self._smoothed.hyper, raw_scores.hyper)
        self._smoothed.recovery = self._ema(self._smoothed.recovery, raw_scores.recovery)

        # C.2: blend per-user ML classifier into the HYPER channel.
        blended_classifier_source = "rule"
        blended_alpha = 0.0
        if ml_p_hyper is not None and 0.0 < ml_alpha <= 1.0:
            blended_alpha = float(max(0.0, min(1.0, ml_alpha)))
            self._smoothed.hyper = (
                self._smoothed.hyper * (1.0 - blended_alpha)
                + float(ml_p_hyper) * blended_alpha
            )
            blended_classifier_source = "ensemble"

        # Determine dominant state from raw smoothed scores for hysteresis.
        # Probabilities are used for confidence reporting, not entry gating.
        probs = self._compute_probabilities()
        dominant_state, dominant_score = self._smoothed.dominant()

        # Apply hysteresis and dwell time
        confirmed_state = self._apply_hysteresis(dominant_state, dominant_score, now)

        # Update dwell time
        if confirmed_state == self._current_state:
            self._dwell_seconds = now - self._state_entered_at
        else:
            # State changed — record transition
            transition = StateTransition(
                timestamp=now,
                from_state=self._current_state.value,
                to_state=confirmed_state.value,
                from_confidence=self._get_state_score(self._current_state),
                to_confidence=self._get_state_score(confirmed_state),
                dwell_seconds=self._dwell_seconds,
                trigger_reasons=self._generate_reasons(),
            )
            self._transitions.append(transition)
            logger.info(
                f"State transition: {self._current_state.value} → {confirmed_state.value} "
                f"(confidence={dominant_score:.2f}, dwell={self._dwell_seconds:.1f}s)"
            )

            self._current_state = confirmed_state
            self._state_entered_at = now
            self._dwell_seconds = 0.0

        # C.1: confidence is the raw smoothed dominant score, not a
        # softmax probability. Softmax over 4 saturated [0,1] scores caps
        # at ~0.475, making the spec's 0.85 gate mathematically unreachable
        # and silently suppressing triggers.
        #
        # ``calibrated_probabilities`` is still populated so the dashboard
        # transparency UI can render proportional bars — but the trigger
        # policy and any UI threshold compare against the raw confidence.
        smoothed_state_scores = self._smoothed.to_state_scores()
        confidence_raw = float(
            getattr(smoothed_state_scores, self._current_state.value.lower())
        )
        estimate = StateEstimate(
            state=self._current_state.value,
            confidence=confidence_raw,
            scores=smoothed_state_scores,
            calibrated_probabilities=StateScores(
                flow=probs[UserState.FLOW],
                hypo=probs[UserState.HYPO],
                hyper=probs[UserState.HYPER],
                recovery=probs[UserState.RECOVERY],
            ),
            classifier_source=blended_classifier_source,
            classifier_alpha=blended_alpha,
            reasons=self._generate_reasons(),
            signal_quality=signal_quality,
            timestamp=now,
            dwell_seconds=self._dwell_seconds,
        )

        self._latest = estimate
        return estimate

    def _ema(self, prev: float, new: float) -> float:
        """Apply exponential moving average."""
        return self._alpha * new + (1.0 - self._alpha) * prev

    def _apply_hysteresis(
        self, dominant: UserState, score: float, now: float,
    ) -> UserState:
        """
        Apply hysteresis thresholds and dwell time enforcement.

        A state transition requires:
        1. Dominant score exceeds entry threshold (0.85)
        2. Current state score drops below exit threshold (0.70)
        3. Candidate state maintained for required dwell time
        """
        current_score = self._get_state_score(self._current_state)

        # Check if we should even consider a transition
        if dominant == self._current_state:
            # Same state — no transition needed
            self._candidate_state = None
            return self._current_state

        # Check exit condition: current state must have weakened below exit_threshold
        # (proper Schmitt trigger — separate entry and exit thresholds)
        if current_score > self._config.exit_threshold:
            # Current state still strong — no transition
            self._candidate_state = None
            return self._current_state

        # Check entry condition: new state must exceed entry threshold
        if score < self._config.entry_threshold:
            # New state not strong enough — no transition
            # But allow FLOW and RECOVERY to transition more easily
            if dominant in (UserState.FLOW, UserState.RECOVERY) and score >= 0.5:
                pass  # Allow weaker transitions to flow/recovery
            else:
                self._candidate_state = None
                return self._current_state

        # Check dwell time
        if self._candidate_state != dominant:
            # New candidate — start dwell timer
            self._candidate_state = dominant
            self._candidate_since = now
            return self._current_state

        # Same candidate — check if dwell time is met
        dwell_required = self._get_dwell_time(dominant)
        elapsed = now - self._candidate_since

        if elapsed >= dwell_required:
            # Dwell time met — confirm transition
            self._candidate_state = None
            return dominant

        return self._current_state

    def _get_state_score(self, state: UserState) -> float:
        """Get smoothed score for a specific state."""
        scores = {
            UserState.FLOW: self._smoothed.flow,
            UserState.HYPO: self._smoothed.hypo,
            UserState.HYPER: self._smoothed.hyper,
            UserState.RECOVERY: self._smoothed.recovery,
        }
        return scores[state]

    def _compute_probabilities(self) -> dict[UserState, float]:
        """Convert smoothed scores into calibrated probabilities via softmax."""
        logits = np.array(
            [
                self._smoothed.flow,
                self._smoothed.hypo,
                self._smoothed.hyper,
                self._smoothed.recovery,
            ],
            dtype=np.float64,
        )
        temp = max(1e-3, float(self._probability_temperature))
        logits = logits / temp
        logits = logits - np.max(logits)
        exp = np.exp(logits)
        denom = float(np.sum(exp)) if np.sum(exp) > 1e-12 else 1.0
        probs = exp / denom
        return {
            UserState.FLOW: float(probs[0]),
            UserState.HYPO: float(probs[1]),
            UserState.HYPER: float(probs[2]),
            UserState.RECOVERY: float(probs[3]),
        }

    def _get_dwell_time(self, state: UserState) -> float:
        """Get required dwell time for a state transition."""
        dwell_map = {
            UserState.HYPER: self._config.hyper_dwell_seconds,
            UserState.HYPO: self._config.hypo_dwell_seconds,
            UserState.FLOW: self._config.flow_dwell_seconds,
            UserState.RECOVERY: 5.0,  # Faster recovery transitions
        }
        return float(dwell_map.get(state, 8.0))

    def _generate_reasons(self) -> list[str]:
        """Generate human-readable reasons for the current state."""
        reasons = []
        state = self._current_state

        if state == UserState.HYPER:
            if self._smoothed.hyper > 0.7:
                reasons.append("Elevated overwhelm indicators detected")
            if self._smoothed.hyper > 0.85:
                reasons.append("Multiple stress signals converging")

        elif state == UserState.HYPO:
            if self._smoothed.hypo > 0.6:
                reasons.append("Low engagement indicators detected")

        elif state == UserState.FLOW:
            if self._smoothed.flow > 0.7:
                reasons.append("Stable, focused engagement pattern")

        elif state == UserState.RECOVERY:
            reasons.append("Transitioning from elevated state")

        return reasons

    def reset(self) -> None:
        """Reset smoother state."""
        self._smoothed = SmoothedScores()
        self._current_state = UserState.FLOW
        self._state_entered_at = None
        self._dwell_seconds = 0.0
        self._candidate_state = None
        self._candidate_since = 0.0
        self._transitions.clear()
        self._latest = None
