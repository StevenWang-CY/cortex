"""Temporal smoothing, hysteresis, and evidence-status handling.

The smoother starts UNKNOWN, consumes monotonic time, and never converts rule
scores to probabilities. Recovery is represented only after a confirmed
support-likely episode begins to subside; it is not inferred from a mixed
single-frame feature vector.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass

from cortex.application.clock import SYSTEM_CLOCK, Clock, monotonic_seconds
from cortex.libs.config.settings import StateConfig
from cortex.libs.logging.correlation import get_correlation_id
from cortex.libs.logging.structured import log_state_transition
from cortex.libs.observability.metrics import STATE_TRANSITIONS_TOTAL
from cortex.libs.schemas.state import (
    EstimateStatus,
    FeatureContribution,
    RuleEvaluation,
    SignalQuality,
    StateEstimate,
    StateScores,
    StateTransition,
    SupportScores,
    SupportState,
    UserState,
    legacy_inference_identity,
)
from cortex.libs.schemas.temporal import EventTime

logger = logging.getLogger(__name__)


@dataclass
class SmoothedScores:
    """EMA-smoothed heuristic scores. All channels start with no evidence."""

    flow: float = 0.0
    hypo: float = 0.0
    hyper: float = 0.0
    recovery: float = 0.0

    def to_state_scores(self) -> StateScores:
        return StateScores(
            flow=self.flow,
            hypo=self.hypo,
            hyper=self.hyper,
            recovery=self.recovery,
        )

    def to_support_scores(self) -> SupportScores:
        return SupportScores(
            support_likely=self.hyper,
            under_engaged=self.hypo,
            flow_like=self.flow,
            recovering=self.recovery,
        )

    def dominant(self) -> tuple[UserState, float]:
        return self.to_state_scores().dominant_state()


_SUPPORT_TO_LEGACY = {
    SupportState.SUPPORT_LIKELY: UserState.HYPER,
    SupportState.UNDER_ENGAGED: UserState.HYPO,
    SupportState.FLOW_LIKE: UserState.FLOW,
    SupportState.RECOVERING: UserState.RECOVERY,
    SupportState.UNKNOWN: UserState.UNKNOWN,
}
_LEGACY_TO_SUPPORT = {value: key for key, value in _SUPPORT_TO_LEGACY.items()}


class ScoreSmoother:
    """Apply EMA, Schmitt hysteresis, and elapsed-time dwell confirmation."""

    def __init__(
        self,
        config: StateConfig | None = None,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._config = config or StateConfig()
        self._clock = clock or SYSTEM_CLOCK
        self._alpha = self._config.ema_alpha
        self._smoothed = SmoothedScores()
        self._current_state = UserState.UNKNOWN
        self._state_entered_at: float | None = None
        self._dwell_seconds = 0.0
        self._candidate_state: UserState | None = None
        self._candidate_since = 0.0
        self._last_update_at: float | None = None
        self._transitions: deque[StateTransition] = deque(maxlen=100)
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
        raw_scores: StateScores | RuleEvaluation,
        signal_quality: SignalQuality,
        timestamp: float | None = None,
        *,
        event_time: EventTime | None = None,
    ) -> StateEstimate:
        """Consume a rule evaluation and publish a support estimate."""
        if event_time is not None:
            supplied_now = event_time.observed_at_mono_ns / 1_000_000_000.0
        else:
            supplied_now = (
                timestamp if timestamp is not None else monotonic_seconds(self._clock)
            )
        # A replayed/regressed timestamp must not create negative dwell or move
        # a candidate timer backward. The event ordering layer handles drops;
        # this clamp is the final domain invariant.
        now = (
            max(supplied_now, self._last_update_at)
            if self._last_update_at is not None
            else supplied_now
        )
        self._last_update_at = now
        if self._state_entered_at is None:
            self._state_entered_at = now

        evaluation = self._coerce_evaluation(raw_scores, signal_quality)
        if evaluation.status != EstimateStatus.ESTIMATED:
            self._decay_scores()
            self._candidate_state = None
            self._force_unknown(now, event_time)
            estimate = self._build_estimate(
                evaluation=evaluation,
                signal_quality=signal_quality,
                now=now,
                event_time=event_time,
                status=EstimateStatus(str(evaluation.status)),
            )
            self._latest = estimate
            return estimate

        support_scores = evaluation.scores
        self._smoothed.flow = self._ema(self._smoothed.flow, support_scores.flow_like)
        self._smoothed.hypo = self._ema(
            self._smoothed.hypo, support_scores.under_engaged
        )
        self._smoothed.hyper = self._ema(
            self._smoothed.hyper, support_scores.support_likely
        )
        self._smoothed.recovery = self._ema(self._smoothed.recovery, 0.0)

        dominant_state, dominant_score = self._smoothed.dominant()
        # Recovery is temporal: it exists only while leaving a confirmed
        # support-likely episode toward a sufficiently supported alternative.
        if (
            self._current_state == UserState.HYPER
            and self._smoothed.hyper < self._config.estimate_exit_threshold
            and dominant_state not in (UserState.HYPER, UserState.UNKNOWN)
        ):
            recovery_score = max(self._smoothed.flow, self._smoothed.hypo) * (
                1.0 - self._smoothed.hyper
            )
            self._smoothed.recovery = max(self._smoothed.recovery, recovery_score)
            dominant_state = UserState.RECOVERY
            dominant_score = self._smoothed.recovery

        confirmed = self._apply_hysteresis(dominant_state, dominant_score, now)
        if confirmed != self._current_state:
            self._commit_transition(confirmed, dominant_score, now, event_time)
        else:
            self._dwell_seconds = max(0.0, now - self._state_entered_at)

        # An eligible rule frame may still be waiting out its entry dwell. The
        # UI must render that as warming, not as a confident UNKNOWN label.
        output_status = (
            EstimateStatus.WARMING_UP
            if self._current_state == UserState.UNKNOWN
            else EstimateStatus.ESTIMATED
        )
        estimate = self._build_estimate(
            evaluation=evaluation,
            signal_quality=signal_quality,
            now=now,
            event_time=event_time,
            status=output_status,
        )
        self._latest = estimate
        return estimate

    def _coerce_evaluation(
        self,
        raw: StateScores | RuleEvaluation,
        quality: SignalQuality,
    ) -> RuleEvaluation:
        if isinstance(raw, RuleEvaluation):
            return raw
        status = (
            EstimateStatus.ESTIMATED
            if quality.acceptable
            else EstimateStatus.INSUFFICIENT_EVIDENCE
        )
        return RuleEvaluation(
            status=status,
            scores=SupportScores(
                support_likely=raw.hyper,
                under_engaged=raw.hypo,
                flow_like=raw.flow,
                recovering=0.0,
            ),
            evidence_coverage=quality.overall,
            state_coverage={
                SupportState.SUPPORT_LIKELY: quality.overall,
                SupportState.UNDER_ENGAGED: quality.overall,
                SupportState.FLOW_LIKE: quality.overall,
                SupportState.RECOVERING: 0.0,
                SupportState.UNKNOWN: 0.0,
            },
            exclusions=["Legacy score-only input omitted per-feature provenance."],
            model=legacy_inference_identity(),
        )

    def _build_estimate(
        self,
        *,
        evaluation: RuleEvaluation,
        signal_quality: SignalQuality,
        now: float,
        event_time: EventTime | None,
        status: EstimateStatus,
    ) -> StateEstimate:
        state = self._current_state if status == EstimateStatus.ESTIMATED else UserState.UNKNOWN
        support_state = _LEGACY_TO_SUPPORT[state]
        state_scores = self._smoothed.to_state_scores()
        support_scores = self._smoothed.to_support_scores()
        confidence = self._get_state_score(state) if state != UserState.UNKNOWN else 0.0
        return StateEstimate(
            state=state,
            support_state=support_state,
            status=status,
            confidence=confidence,
            scores=state_scores,
            support_scores=support_scores,
            evidence_coverage=evaluation.evidence_coverage,
            contributing_features=evaluation.contributing_features,
            exclusions=evaluation.exclusions,
            model=evaluation.model,
            probabilities=None,
            calibrated_probabilities=None,
            classifier_source="rule",
            classifier_alpha=0.0,
            reasons=self._generate_reasons(status, evaluation.contributing_features),
            signal_quality=signal_quality,
            timestamp=now,
            observed_at_unix_ms=(
                event_time.observed_at_unix_ms if event_time is not None else None
            ),
            observed_at_mono_ns=(
                event_time.observed_at_mono_ns if event_time is not None else None
            ),
            boot_id=event_time.boot_id if event_time is not None else None,
            dwell_seconds=self._dwell_seconds if state != UserState.UNKNOWN else 0.0,
        )

    def _ema(self, previous: float, current: float) -> float:
        return self._alpha * current + (1.0 - self._alpha) * previous

    def _decay_scores(self) -> None:
        self._smoothed.flow = self._ema(self._smoothed.flow, 0.0)
        self._smoothed.hypo = self._ema(self._smoothed.hypo, 0.0)
        self._smoothed.hyper = self._ema(self._smoothed.hyper, 0.0)
        self._smoothed.recovery = self._ema(self._smoothed.recovery, 0.0)

    def _force_unknown(self, now: float, event_time: EventTime | None) -> None:
        if self._current_state != UserState.UNKNOWN:
            self._commit_transition(UserState.UNKNOWN, 0.0, now, event_time)
        else:
            self._dwell_seconds = 0.0
            self._state_entered_at = now

    def _apply_hysteresis(
        self,
        dominant: UserState,
        score: float,
        now: float,
    ) -> UserState:
        if dominant == UserState.UNKNOWN:
            self._candidate_state = None
            return self._current_state
        if dominant == self._current_state:
            self._candidate_state = None
            return self._current_state

        current_score = self._get_state_score(self._current_state)
        if (
            self._current_state != UserState.UNKNOWN
            and current_score > self._config.estimate_exit_threshold
        ):
            self._candidate_state = None
            return self._current_state

        entry_threshold = self._config.estimate_entry_threshold
        if dominant == UserState.RECOVERY:
            entry_threshold = min(entry_threshold, 0.5)
        if score < entry_threshold:
            self._candidate_state = None
            return self._current_state

        if self._candidate_state != dominant:
            self._candidate_state = dominant
            self._candidate_since = now
            return self._current_state
        if now - self._candidate_since < self._get_dwell_time(dominant):
            return self._current_state
        self._candidate_state = None
        return dominant

    def _commit_transition(
        self,
        new_state: UserState,
        score: float,
        now: float,
        event_time: EventTime | None,
    ) -> None:
        old_state = self._current_state
        transition = StateTransition(
            timestamp=now,
            observed_at_unix_ms=(
                event_time.observed_at_unix_ms if event_time is not None else None
            ),
            observed_at_mono_ns=(
                event_time.observed_at_mono_ns if event_time is not None else None
            ),
            boot_id=event_time.boot_id if event_time is not None else None,
            from_state=old_state,
            to_state=new_state,
            from_confidence=self._get_state_score(old_state),
            to_confidence=max(0.0, min(1.0, score)),
            dwell_seconds=self._dwell_seconds,
            trigger_reasons=self._generate_reasons(EstimateStatus.ESTIMATED, []),
        )
        self._transitions.append(transition)
        STATE_TRANSITIONS_TOTAL.labels(
            from_state=old_state.value,
            to_state=new_state.value,
        ).inc()
        log_state_transition(
            from_state=old_state.value,
            to_state=new_state.value,
            confidence=max(0.0, min(1.0, score)),
            reasons=transition.trigger_reasons,
            dwell_seconds=self._dwell_seconds,
            correlation_id=get_correlation_id(),
        )
        self._current_state = new_state
        self._state_entered_at = now
        self._dwell_seconds = 0.0

    def _get_state_score(self, state: UserState) -> float:
        return {
            UserState.UNKNOWN: 0.0,
            UserState.FLOW: self._smoothed.flow,
            UserState.HYPO: self._smoothed.hypo,
            UserState.HYPER: self._smoothed.hyper,
            UserState.RECOVERY: self._smoothed.recovery,
        }[state]

    def _get_dwell_time(self, state: UserState) -> float:
        return float({
            UserState.UNKNOWN: 0.0,
            UserState.HYPER: self._config.hyper_dwell_seconds,
            UserState.HYPO: self._config.hypo_dwell_seconds,
            UserState.FLOW: self._config.flow_dwell_seconds,
            UserState.RECOVERY: 5.0,
        }[state])

    def _generate_reasons(
        self,
        status: EstimateStatus,
        contributions: list[FeatureContribution],
    ) -> list[str]:
        if status == EstimateStatus.WARMING_UP:
            return ["Still gathering enough input-pattern evidence"]
        if status == EstimateStatus.INSUFFICIENT_EVIDENCE:
            return ["Not enough current evidence for a support estimate"]
        positive = sorted(
            (
                item
                for item in contributions
                if item.observed and item.direction == "positive"
            ),
            key=lambda item: abs(item.contribution),
            reverse=True,
        )
        reasons = [
            f"Observed {item.feature.replace('_', ' ')} contributed to the estimate"
            for item in positive[:2]
        ]
        if reasons:
            return reasons
        return ["Observed input patterns were stable but not diagnostic"]

    def reset(self) -> None:
        self._smoothed = SmoothedScores()
        self._current_state = UserState.UNKNOWN
        self._state_entered_at = None
        self._dwell_seconds = 0.0
        self._candidate_state = None
        self._candidate_since = 0.0
        self._last_update_at = None
        self._transitions.clear()
        self._latest = None
