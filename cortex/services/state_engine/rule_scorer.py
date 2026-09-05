"""Evidence-aware deterministic support scorer.

The production path emits bounded heuristic scores, never probabilities or a
diagnosis. Each score uses a fixed denominator, so removing or lowering the
quality of evidence cannot increase its strength. Webcam-derived physiology,
blink, and head/neck values are intentionally excluded pending the registered
participant-held-out validation protocol.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import numpy as np

from cortex.libs.config.settings import StateConfig
from cortex.libs.schemas.features import FeatureName, FeatureValue, FeatureVector
from cortex.libs.schemas.observations import MissingReason
from cortex.libs.schemas.state import (
    EstimateStatus,
    FeatureContribution,
    RuleEvaluation,
    StateScores,
    SupportScores,
    SupportState,
    UserBaselines,
)
from cortex.services.state_engine.feature_schema import ORDERED_FEATURES
from cortex.services.state_engine.model_registry import deterministic_support_identity

logger = logging.getLogger(__name__)


_SUPPORT_WEIGHTS: dict[FeatureName, float] = {
    FeatureName.MOUSE_VELOCITY_VARIANCE: 0.18,
    FeatureName.CLICK_FREQUENCY: 0.06,
    FeatureName.KEYSTROKE_INTERVAL_VARIANCE: 0.08,
    FeatureName.CORRECTION_RATE_PER_100_KEYS: 0.16,
    FeatureName.TAB_SWITCH_RATE_PER_MIN: 0.18,
    FeatureName.SCROLL_BACK_RATE_PER_MIN: 0.12,
    FeatureName.THRASHING_SCORE: 0.22,
}
_FLOW_WEIGHTS: dict[FeatureName, float] = {
    FeatureName.MOUSE_VELOCITY_MEAN: 0.07,
    FeatureName.MOUSE_VELOCITY_VARIANCE: 0.13,
    FeatureName.CLICK_FREQUENCY: 0.05,
    FeatureName.KEYPRESS_RATE_PER_MIN: 0.15,
    FeatureName.KEYSTROKE_INTERVAL_VARIANCE: 0.09,
    FeatureName.CORRECTION_RATE_PER_100_KEYS: 0.13,
    FeatureName.TAB_SWITCH_RATE_PER_MIN: 0.14,
    FeatureName.SCROLL_BACK_RATE_PER_MIN: 0.09,
    FeatureName.THRASHING_SCORE: 0.15,
}
_UNDER_ENGAGED_WEIGHTS: dict[FeatureName, float] = {
    FeatureName.INACTIVITY_SECONDS: 0.45,
    FeatureName.MOUSE_VELOCITY_MEAN: 0.15,
    FeatureName.CLICK_FREQUENCY: 0.10,
    FeatureName.KEYPRESS_RATE_PER_MIN: 0.15,
    FeatureName.TAB_SWITCH_RATE_PER_MIN: 0.15,
}
_PRODUCTION_FEATURE_NAMES = frozenset(
    set(_SUPPORT_WEIGHTS) | set(_FLOW_WEIGHTS) | set(_UNDER_ENGAGED_WEIGHTS)
)

for _rule_name, _rule_weights in (
    ("support_likely", _SUPPORT_WEIGHTS),
    ("flow_like", _FLOW_WEIGHTS),
    ("under_engaged", _UNDER_ENGAGED_WEIGHTS),
):
    if not np.isclose(sum(_rule_weights.values()), 1.0):
        raise RuntimeError(f"{_rule_name} weights must sum to 1.0")


class RuleScorer:
    """
    Computes state scores from FeatureVector using rule-based sub-scorers.

    Each sub-scorer maps a specific feature (or feature combination) to a
    0-1 score indicating the degree to which that feature suggests a
    particular cognitive state.

    Usage:
        scorer = RuleScorer(baselines=user_baselines)
        scores = scorer.compute_scores(feature_vector)
    """

    def __init__(
        self,
        config: StateConfig | None = None,
        baselines: UserBaselines | None = None,
    ) -> None:
        self._config = config or StateConfig()
        self._baselines = baselines or UserBaselines()
        # Optional tab category context for same-category discount
        self._tab_categories: list[str] | None = None

    @property
    def baselines(self) -> UserBaselines:
        return self._baselines

    @baselines.setter
    def baselines(self, value: UserBaselines) -> None:
        self._baselines = value

    def set_tab_categories(self, categories: list[str] | None) -> None:
        """Set the current tab categories for same-category switching discount.

        When the user is switching between tabs that all belong to the same
        category (e.g., all ``educational``), the window-switching penalty
        should be reduced because the switching is topically coherent.

        Args:
            categories: List of tab type strings (one per open tab),
                or None to disable the discount.
        """
        self._tab_categories = categories

    def _same_category_ratio(self) -> float:
        """Compute the ratio of tabs sharing the most common category.

        Returns 0.0 when no tab categories are set or there is only one tab,
        and approaches 1.0 when all tabs share the same type.
        """
        cats = self._tab_categories
        if not cats or len(cats) < 2:
            return 0.0
        from collections import Counter
        counts = Counter(cats)
        # B22 (Phase 4.1): guard ``most_common(1)`` against empty input.
        # ``Counter()`` on a non-empty list always returns at least one
        # entry, but if a filtering step ever produces an empty Counter
        # (e.g. all categories are sentinels), ``most_common(1)[0][1]``
        # would raise IndexError. Defensive zero-return matches the
        # contract documented above: "0.0 when no tab categories…".
        top = counts.most_common(1)
        if not top:
            return 0.0
        most_common_count = top[0][1]
        return most_common_count / len(cats)

    def compute_scores(self, fv: FeatureVector) -> StateScores:
        """Return the one-cycle uppercase projection of Level-A scores."""

        return self.evaluate(fv).scores.to_legacy()

    def evaluate(self, fv: FeatureVector) -> RuleEvaluation:
        """Evaluate observed evidence under the registered Level-A rules."""

        features = self._canonical_features(fv)
        contributions: list[FeatureContribution] = []
        exclusions = [
            f"{definition.name.value}: {definition.exclusion_reason}"
            for definition in ORDERED_FEATURES
            if definition.exclusion_reason is not None
        ]

        support, support_coverage, support_count = self._score_rule(
            SupportState.SUPPORT_LIKELY,
            _SUPPORT_WEIGHTS,
            features,
            self._support_transform,
            contributions,
        )
        flow, flow_coverage, flow_count = self._score_rule(
            SupportState.FLOW_LIKE,
            _FLOW_WEIGHTS,
            features,
            self._flow_transform,
            contributions,
        )
        under, under_coverage, under_count = self._score_rule(
            SupportState.UNDER_ENGAGED,
            _UNDER_ENGAGED_WEIGHTS,
            features,
            self._under_engaged_transform,
            contributions,
        )

        # Quiet input is ambiguous. Under-engaged requires an observed period
        # of inactivity plus at least one corroborating behavior channel.
        inactivity = features.get(FeatureName.INACTIVITY_SECONDS)
        inactivity_strength = (
            self._under_engaged_transform(
                FeatureName.INACTIVITY_SECONDS, float(inactivity.value)
            )
            if inactivity is not None and inactivity.valid and inactivity.value is not None
            else 0.0
        )
        if inactivity_strength <= 0.0 or under_count < 2:
            under = 0.0

        # Stable zero-valued streams do not establish steady activity.
        # Require a recent-input observation and an affirmative interaction
        # channel so telemetry availability cannot be mistaken for work.
        recent_activity = bool(
            inactivity is not None
            and inactivity.valid
            and inactivity.value is not None
            and inactivity.value <= 30.0
        )
        affirmative_activity = any(
            feature is not None
            and feature.valid
            and feature.value is not None
            and float(feature.value) >= threshold
            for feature, threshold in (
                (features.get(FeatureName.MOUSE_VELOCITY_MEAN), 25.0),
                (features.get(FeatureName.CLICK_FREQUENCY), 0.02),
                (features.get(FeatureName.KEYPRESS_RATE_PER_MIN), 2.0),
            )
        )
        if not recent_activity or not affirmative_activity:
            flow = 0.0

        minimum_coverage = 0.45
        eligible = {
            SupportState.SUPPORT_LIKELY: (
                support if support_count >= 3 and support_coverage >= minimum_coverage else 0.0
            ),
            SupportState.FLOW_LIKE: (
                flow if flow_count >= 3 and flow_coverage >= minimum_coverage else 0.0
            ),
            SupportState.UNDER_ENGAGED: (
                under if under_count >= 2 and under_coverage >= minimum_coverage else 0.0
            ),
        }
        coverage_by_state = {
            SupportState.SUPPORT_LIKELY: support_coverage,
            SupportState.UNDER_ENGAGED: under_coverage,
            SupportState.FLOW_LIKE: flow_coverage,
            SupportState.RECOVERING: 0.0,
            SupportState.UNKNOWN: 0.0,
        }
        scores = SupportScores(
            support_likely=eligible[SupportState.SUPPORT_LIKELY],
            under_engaged=eligible[SupportState.UNDER_ENGAGED],
            flow_like=eligible[SupportState.FLOW_LIKE],
            # Recovery is a temporal relation and is owned by ScoreSmoother.
            recovering=0.0,
        )
        dominant, strength = scores.dominant()
        evidence_coverage = (
            coverage_by_state[dominant] if dominant != SupportState.UNKNOWN else 0.0
        )
        valid_production_count = sum(
            1
            for name, feature in features.items()
            if name in _PRODUCTION_FEATURE_NAMES and feature.valid
        )
        if fv.telemetry_seen_count < 5:
            status = EstimateStatus.WARMING_UP
            exclusions.append("Telemetry warm-up requires five observed snapshots.")
        elif valid_production_count < 2 or not any(eligible.values()):
            status = EstimateStatus.INSUFFICIENT_EVIDENCE
        elif strength < 0.25:
            status = EstimateStatus.INSUFFICIENT_EVIDENCE
            exclusions.append("Observed patterns are too ambiguous for a support label.")
        else:
            status = EstimateStatus.ESTIMATED

        return RuleEvaluation(
            status=status,
            scores=scores,
            evidence_coverage=evidence_coverage,
            state_coverage=coverage_by_state,
            contributing_features=contributions,
            exclusions=exclusions,
            model=deterministic_support_identity(),
        )

    def _score_rule(
        self,
        state: SupportState,
        weights: dict[FeatureName, float],
        features: dict[FeatureName, FeatureValue],
        transform: Callable[[FeatureName, float], float],
        contributions: list[FeatureContribution],
    ) -> tuple[float, float, int]:
        score = 0.0
        coverage = 0.0
        observed_count = 0
        for name, weight in weights.items():
            feature = features.get(name)
            if feature is None or not feature.valid or feature.value is None:
                contributions.append(FeatureContribution(
                    feature=name.value,
                    support_state=state,
                    direction="missing",
                    contribution=0.0,
                    quality=0.0,
                    observed=False,
                    note="Feature unavailable; it contributes neither evidence nor score.",
                ))
                continue
            observed_count += 1
            quality = float(feature.quality)
            transformed = float(transform(name, float(feature.value)))
            transformed = float(np.clip(transformed, 0.0, 1.0))
            positive = weight * quality * transformed
            counter = -weight * quality * (1.0 - transformed)
            score += positive
            coverage += weight * quality
            contributions.append(FeatureContribution(
                feature=name.value,
                support_state=state,
                direction="positive" if transformed >= 0.5 else "negative",
                contribution=positive if transformed >= 0.5 else counter,
                quality=quality,
                observed=True,
                note=(
                    f"Fixed weight {weight:.2f}; transform={transformed:.3f}; "
                    "score is quality-bounded and not renormalized."
                ),
            ))
        return (
            float(np.clip(score, 0.0, 1.0)),
            float(np.clip(coverage, 0.0, 1.0)),
            observed_count,
        )

    def _support_transform(self, name: FeatureName, value: float) -> float:
        if name == FeatureName.MOUSE_VELOCITY_VARIANCE:
            return self.score_mouse_thrash(value)
        if name == FeatureName.CLICK_FREQUENCY:
            return self._ramp(value, 0.5, 3.0)
        if name == FeatureName.KEYSTROKE_INTERVAL_VARIANCE:
            return self._ramp(value, 1_500.0, 8_000.0)
        if name == FeatureName.CORRECTION_RATE_PER_100_KEYS:
            return self._ramp(value, 8.0, 25.0)
        if name == FeatureName.TAB_SWITCH_RATE_PER_MIN:
            score = self.score_window_switch(value)
            ratio = self._same_category_ratio()
            return score * (1.0 - 0.5 * ratio) if ratio > 0.6 else score
        if name == FeatureName.SCROLL_BACK_RATE_PER_MIN:
            return self._ramp(value, 10.0, 40.0)
        if name == FeatureName.THRASHING_SCORE:
            return value
        return 0.0

    def _flow_transform(self, name: FeatureName, value: float) -> float:
        if name == FeatureName.MOUSE_VELOCITY_MEAN:
            return self._band(value, 100.0, 800.0, 1_500.0)
        if name == FeatureName.MOUSE_VELOCITY_VARIANCE:
            baseline = max(1.0, self._baselines.mouse_variance_baseline)
            return 1.0 - self._ramp(value / baseline, 1.0, 2.5)
        if name == FeatureName.CLICK_FREQUENCY:
            return self._band(value, 0.05, 1.5, 3.0)
        if name == FeatureName.KEYPRESS_RATE_PER_MIN:
            return self._band(value, 2.0, 120.0, 300.0)
        if name == FeatureName.KEYSTROKE_INTERVAL_VARIANCE:
            return 1.0 - self._ramp(value, 1_500.0, 8_000.0)
        if name == FeatureName.CORRECTION_RATE_PER_100_KEYS:
            return 1.0 - self._ramp(value, 8.0, 25.0)
        if name == FeatureName.TAB_SWITCH_RATE_PER_MIN:
            return self._band(value, 0.5, 4.0, 10.0)
        if name == FeatureName.SCROLL_BACK_RATE_PER_MIN:
            return 1.0 - self._ramp(value, 10.0, 40.0)
        if name == FeatureName.THRASHING_SCORE:
            return 1.0 - value
        return 0.0

    @staticmethod
    def _under_engaged_transform(name: FeatureName, value: float) -> float:
        if name == FeatureName.INACTIVITY_SECONDS:
            return RuleScorer._ramp(value, 30.0, 300.0)
        if name == FeatureName.MOUSE_VELOCITY_MEAN:
            return 1.0 - RuleScorer._ramp(value, 25.0, 250.0)
        if name == FeatureName.CLICK_FREQUENCY:
            return 1.0 - RuleScorer._ramp(value, 0.05, 0.75)
        if name == FeatureName.KEYPRESS_RATE_PER_MIN:
            return 1.0 - RuleScorer._ramp(value, 2.0, 60.0)
        if name == FeatureName.TAB_SWITCH_RATE_PER_MIN:
            return 1.0 - RuleScorer._ramp(value, 0.2, 4.0)
        return 0.0

    @staticmethod
    def _ramp(value: float, low: float, high: float) -> float:
        if high <= low:
            raise ValueError("ramp high bound must exceed low bound")
        return float(np.clip((value - low) / (high - low), 0.0, 1.0))

    @staticmethod
    def _band(value: float, low: float, high: float, outer_high: float) -> float:
        if value < low:
            return float(np.clip(value / max(low, 1e-6), 0.0, 1.0))
        if value <= high:
            return 1.0
        return 1.0 - RuleScorer._ramp(value, high, outer_high)

    def _canonical_features(self, fv: FeatureVector) -> dict[FeatureName, FeatureValue]:
        if fv.features:
            return dict(fv.features)

        # Decode-only bridge for tests and one-release callers still sending
        # the flat v1 shape. Presence is inferred only from an explicit sample
        # count or a non-default telemetry value; zeros alone never fabricate
        # an available source.
        telemetry_present = fv.telemetry_seen_count > 0 or any((
            fv.mouse_velocity_mean != 0.0,
            fv.mouse_velocity_variance != 0.0,
            fv.click_frequency != 0.0,
            fv.keypress_rate_per_min != 0.0,
            fv.keystroke_interval_variance != 0.0,
            fv.correction_rate_per_100_keys is not None,
            fv.tab_switch_frequency != 0.0,
            fv.scroll_back_rate_per_min is not None,
            fv.thrashing_score != 0.0,
        ))

        def legacy(value: float | None, *, present: bool, version: str) -> FeatureValue:
            if present and value is not None:
                return FeatureValue(
                    value=float(value), valid=True, quality=1.0, age_ms=0,
                    source_window_ms=15_000, algorithm_version=version,
                )
            return FeatureValue(
                valid=False, quality=0.0, age_ms=0, source_window_ms=15_000,
                algorithm_version=version,
                missing_reason=MissingReason.SOURCE_DISCONNECTED,
            )

        values = {
            FeatureName.HEART_RATE_BPM: legacy(
                fv.hr, present=fv.hr is not None, version="legacy-flat-v1"
            ),
            FeatureName.BLINK_RATE_PER_MIN: legacy(
                fv.blink_rate,
                present=fv.blink_rate is not None,
                version="legacy-flat-v1",
            ),
            FeatureName.HEAD_NECK_FLEXION_SCORE: legacy(
                fv.head_neck_flexion_score,
                present=fv.head_neck_proxy_available,
                version="legacy-flat-v1",
            ),
            FeatureName.MOUSE_VELOCITY_MEAN: legacy(
                fv.mouse_velocity_mean, present=telemetry_present, version="legacy-flat-v1"
            ),
            FeatureName.MOUSE_VELOCITY_VARIANCE: legacy(
                fv.mouse_velocity_variance,
                present=telemetry_present,
                version="legacy-flat-v1",
            ),
            FeatureName.CLICK_FREQUENCY: legacy(
                fv.click_frequency, present=telemetry_present, version="legacy-flat-v1"
            ),
            FeatureName.KEYPRESS_RATE_PER_MIN: legacy(
                fv.keypress_rate_per_min,
                present=telemetry_present,
                version="legacy-flat-v1",
            ),
            FeatureName.KEYSTROKE_INTERVAL_VARIANCE: legacy(
                fv.keystroke_interval_variance,
                present=telemetry_present,
                version="legacy-flat-v1",
            ),
            FeatureName.CORRECTION_RATE_PER_100_KEYS: legacy(
                fv.correction_rate_per_100_keys,
                present=telemetry_present,
                version="legacy-flat-v1",
            ),
            FeatureName.INACTIVITY_SECONDS: legacy(
                fv.inactivity_seconds,
                present=telemetry_present,
                version="legacy-flat-v1",
            ),
            FeatureName.TAB_SWITCH_RATE_PER_MIN: legacy(
                fv.tab_switch_frequency,
                present=telemetry_present,
                version="legacy-flat-v1",
            ),
            FeatureName.SCROLL_BACK_RATE_PER_MIN: legacy(
                fv.scroll_back_rate_per_min,
                present=telemetry_present,
                version="legacy-flat-v1",
            ),
            FeatureName.THRASHING_SCORE: legacy(
                fv.thrashing_score,
                present=telemetry_present and fv.thrashing_score != 0.0,
                version="legacy-flat-v1",
            ),
        }
        return values

    # =========================================================================
    # Sub-score transforms shared by the Level-A rules (all return 0-1).
    # The pre-v2 physiology/posture scorers (pulse elevation, HRV drop,
    # blink suppression, posture collapse, workspace complexity and the
    # legacy hyper/hypo/flow/recovery composites) were removed: nothing in
    # the production path called them and the model card excludes camera
    # inputs from every production rule (audit D17).
    # =========================================================================

    def score_mouse_thrash(self, velocity_variance: float) -> float:
        """
        Score mouse thrashing: velocity variance > 3x baseline.

        Returns 0-1, where 1.0 = extreme erratic movement.
        """
        # Calibration may persist ``mouse_variance_baseline == 0`` (the schema
        # allows it). Floor it exactly like ``_flow_transform`` so a zero
        # baseline degrades to "any variance is thrash-relative" instead of
        # raising ZeroDivisionError on every state-loop tick (audit D2).
        baseline = max(1.0, float(self._baselines.mouse_variance_baseline))
        if velocity_variance <= baseline:
            return 0.0

        ratio = velocity_variance / baseline
        if ratio <= 3.0:
            # Ramp from 1x to 3x baseline → 0 to 0.5
            return float(np.clip((ratio - 1.0) / 4.0, 0.0, 0.5))

        # Above 3x → 0.5 to 1.0
        score = 0.5 + min(0.5, (ratio - 3.0) / 6.0)
        return float(np.clip(score, 0.0, 1.0))

    def score_window_switch(self, switch_rate: float) -> float:
        """
        Score window switching: > 20 switches/min.

        Returns 0-1, where 1.0 = 40+ switches/min.
        """
        if switch_rate <= 10.0:
            return 0.0

        if switch_rate <= 20.0:
            return float((switch_rate - 10.0) / 20.0 * 0.5)

        # Above 20: 0.5 → 1.0
        score = 0.5 + min(0.5, (switch_rate - 20.0) / 20.0)
        return float(np.clip(score, 0.0, 1.0))
