"""Rule scorer with camera physiology fused against a personal reference.

Behaviour decides which hypotheses are eligible; physiology may only
corroborate or temper an eligible reading, inside the documented bounds.
"""

from __future__ import annotations

import pytest

from cortex.libs.config.settings import StateConfig
from cortex.libs.schemas.features import FeatureName, FeatureValue, FeatureVector
from cortex.libs.schemas.observations import MissingReason
from cortex.libs.schemas.state import (
    EstimateStatus,
    FeatureContribution,
    RuleEvaluation,
    SupportState,
)
from cortex.services.state_engine.feature_schema import FEATURE_DEFINITIONS
from cortex.services.state_engine.physiology_baseline import (
    MetricReference,
    PhysiologyReference,
)
from cortex.services.state_engine.physiology_evidence import (
    NO_PHYSIOLOGY,
    SUPPORT_WEIGHTS,
    UNDER_ENGAGED_WEIGHTS,
)
from cortex.services.state_engine.rule_scorer import RuleScorer


def _valid(name: FeatureName, value: float, quality: float = 1.0) -> FeatureValue:
    return FeatureValue(
        value=value,
        valid=True,
        quality=quality,
        age_ms=0,
        source_window_ms=FEATURE_DEFINITIONS[name].source_window_ms,
        algorithm_version="test-v1",
    )


def _missing(name: FeatureName) -> FeatureValue:
    return FeatureValue(
        valid=False,
        quality=0.0,
        age_ms=0,
        source_window_ms=FEATURE_DEFINITIONS[name].source_window_ms,
        algorithm_version="test-v1",
        missing_reason=MissingReason.SOURCE_DISCONNECTED,
    )


def _vector(values: dict[FeatureName, float], *, telemetry_seen_count: int = 5) -> FeatureVector:
    return FeatureVector(
        timestamp=1.0,
        telemetry_seen_count=telemetry_seen_count,
        features={
            name: _valid(name, values[name]) if name in values else _missing(name)
            for name in FeatureName
        },
    )


def _ready(center: float, spread: float) -> MetricReference:
    return MetricReference(center=center, spread=spread, source="session", ready=True)


REFERENCE = PhysiologyReference(
    heart_rate=_ready(70.0, 5.0),
    sdnn=_ready(50.0, 10.0),
    respiration=_ready(14.0, 2.0),
    blink_rate=_ready(17.0, 4.0),
)

# Every support transform saturates (P = C = 1 for support_likely).
STRAINED_CAMERA: dict[FeatureName, float] = {
    FeatureName.HEART_RATE_BPM: 85.0,
    FeatureName.HRV_SDNN_MS: 25.0,
    FeatureName.RESPIRATION_RATE_BPM: 20.0,
    FeatureName.BLINK_RATE_PER_MIN: 5.1,
    FeatureName.PERCLOS: 0.05,
}
# Everything at the personal reference (P = 0, C = 1 for support_likely).
CALM_CAMERA: dict[FeatureName, float] = {
    FeatureName.HEART_RATE_BPM: 70.0,
    FeatureName.HRV_SDNN_MS: 50.0,
    FeatureName.RESPIRATION_RATE_BPM: 14.0,
    FeatureName.BLINK_RATE_PER_MIN: 17.0,
    FeatureName.PERCLOS: 0.05,
}

# Mid-range support behaviour so the score has room to move both ways.
MODERATE_SUPPORT: dict[FeatureName, float] = {
    FeatureName.MOUSE_VELOCITY_MEAN: 400.0,
    FeatureName.MOUSE_VELOCITY_VARIANCE: 25_000.0,
    FeatureName.CLICK_FREQUENCY: 1.75,
    FeatureName.KEYPRESS_RATE_PER_MIN: 60.0,
    FeatureName.KEYSTROKE_INTERVAL_VARIANCE: 4_750.0,
    FeatureName.CORRECTION_RATE_PER_100_KEYS: 16.5,
    FeatureName.INACTIVITY_SECONDS: 1.0,
    FeatureName.TAB_SWITCH_RATE_PER_MIN: 20.0,
    FeatureName.SCROLL_BACK_RATE_PER_MIN: 25.0,
    FeatureName.THRASHING_SCORE: 0.5,
}
STEADY_FLOW: dict[FeatureName, float] = {
    FeatureName.MOUSE_VELOCITY_MEAN: 400.0,
    FeatureName.MOUSE_VELOCITY_VARIANCE: 5_000.0,
    FeatureName.CLICK_FREQUENCY: 0.5,
    FeatureName.KEYPRESS_RATE_PER_MIN: 60.0,
    FeatureName.KEYSTROKE_INTERVAL_VARIANCE: 500.0,
    FeatureName.CORRECTION_RATE_PER_100_KEYS: 5.0,
    FeatureName.INACTIVITY_SECONDS: 2.0,
    FeatureName.TAB_SWITCH_RATE_PER_MIN: 3.0,
    FeatureName.SCROLL_BACK_RATE_PER_MIN: 1.0,
    FeatureName.THRASHING_SCORE: 0.0,
}
QUIET: dict[FeatureName, float] = {
    FeatureName.INACTIVITY_SECONDS: 200.0,
    FeatureName.MOUSE_VELOCITY_MEAN: 0.0,
    FeatureName.CLICK_FREQUENCY: 0.0,
    FeatureName.KEYPRESS_RATE_PER_MIN: 0.0,
    FeatureName.TAB_SWITCH_RATE_PER_MIN: 0.0,
}
# Flow is eligible (weakly) while the support rule sees only two of its
# features and is therefore ineligible.
WEAK_FLOW_SUPPORT_INELIGIBLE: dict[FeatureName, float] = {
    FeatureName.MOUSE_VELOCITY_MEAN: 30.0,
    FeatureName.KEYPRESS_RATE_PER_MIN: 1.0,
    FeatureName.INACTIVITY_SECONDS: 2.0,
    FeatureName.TAB_SWITCH_RATE_PER_MIN: 8.0,
    FeatureName.THRASHING_SCORE: 0.0,
}


def _scorer(config: StateConfig | None = None) -> RuleScorer:
    scorer = RuleScorer(config=config)
    scorer.set_physiology_reference(REFERENCE)
    return scorer


def _behaviour_only(values: dict[FeatureName, float]) -> RuleEvaluation:
    return RuleScorer().evaluate(_vector(values))


@pytest.mark.parametrize(
    "behaviour",
    [MODERATE_SUPPORT, STEADY_FLOW, QUIET, {}],
    ids=["support", "flow", "quiet", "no-behaviour"],
)
@pytest.mark.parametrize("camera", [STRAINED_CAMERA, CALM_CAMERA], ids=["strained", "calm"])
def test_without_a_reference_camera_features_change_nothing(
    behaviour: dict[FeatureName, float], camera: dict[FeatureName, float]
) -> None:
    """Camera-absent invariance: no reference means the behaviour-only evaluation."""

    scorer = RuleScorer()
    without = scorer.evaluate(_vector(behaviour))
    with_camera = scorer.evaluate(_vector({**behaviour, **camera}))
    assert with_camera.model_dump() == without.model_dump()
    assert scorer.last_physiology is NO_PHYSIOLOGY


def test_strained_physiology_raises_and_calm_lowers_support_within_bounds() -> None:
    scorer = _scorer()
    weight = scorer.physiology_weight
    behaviour = _behaviour_only(MODERATE_SUPPORT)
    b = behaviour.scores.support_likely
    assert 0.2 < b < 0.8, "fixture must leave the score room to move"

    strained = scorer.evaluate(_vector({**MODERATE_SUPPORT, **STRAINED_CAMERA}))
    calm = scorer.evaluate(_vector({**MODERATE_SUPPORT, **CALM_CAMERA}))
    raised, lowered = strained.scores.support_likely, calm.scores.support_likely

    assert raised > b > lowered
    for fused in (raised, lowered):
        assert (1.0 - weight) * b - 1e-9 <= fused <= (1.0 - weight) * b + weight + 1e-9
    # Fully observed and saturated, the two hit the bounds themselves.
    assert raised == pytest.approx((1.0 - weight) * b + weight)
    assert lowered == pytest.approx((1.0 - weight) * b)
    assert strained.status == calm.status == EstimateStatus.ESTIMATED


def test_physiology_cannot_make_an_estimate_without_eligible_behaviour() -> None:
    scorer = _scorer()
    camera_only = scorer.evaluate(_vector(STRAINED_CAMERA))
    assert camera_only.status == EstimateStatus.INSUFFICIENT_EVIDENCE
    assert camera_only.scores.support_likely == 0.0
    assert camera_only.scores.flow_like == 0.0
    assert camera_only.scores.under_engaged == 0.0

    # Two support features only: the support rule abstains, physiology cannot fill in.
    sparse = {
        FeatureName.THRASHING_SCORE: 1.0,
        FeatureName.TAB_SWITCH_RATE_PER_MIN: 40.0,
    }
    sparse_eval = scorer.evaluate(_vector({**sparse, **STRAINED_CAMERA}))
    assert sparse_eval.status == EstimateStatus.INSUFFICIENT_EVIDENCE
    assert sparse_eval.scores.support_likely == 0.0

    warming = scorer.evaluate(
        _vector({**MODERATE_SUPPORT, **STRAINED_CAMERA}, telemetry_seen_count=2)
    )
    assert warming.status == EstimateStatus.WARMING_UP


def test_ineligible_state_is_not_fused_even_when_another_state_is() -> None:
    """Only a state whose own behaviour rule scored is fused; the others stay untouched."""

    scorer = _scorer()
    behaviour = _behaviour_only(WEAK_FLOW_SUPPORT_INELIGIBLE)
    assert behaviour.scores.support_likely == 0.0
    assert behaviour.scores.under_engaged == 0.0
    assert behaviour.scores.flow_like > 0.0
    fused = scorer.evaluate(_vector({**WEAK_FLOW_SUPPORT_INELIGIBLE, **STRAINED_CAMERA}))
    # Strained physiology would push support hardest, yet the state stays exactly zero.
    assert scorer.last_physiology.support_likely.score == pytest.approx(1.0)
    assert fused.scores.support_likely == 0.0
    assert fused.scores.under_engaged == 0.0
    for state in (SupportState.SUPPORT_LIKELY, SupportState.UNDER_ENGAGED):
        assert fused.state_coverage[state] == behaviour.state_coverage[state]
    # The eligible state is fused (strain tempers flow).
    assert fused.scores.flow_like < behaviour.scores.flow_like
    assert fused.state_coverage[SupportState.FLOW_LIKE] > behaviour.state_coverage[
        SupportState.FLOW_LIKE
    ]
    listed = {
        item.support_state
        for item in fused.contributing_features
        if item.feature in {name.value for name in STRAINED_CAMERA}
    }
    assert listed == {SupportState.FLOW_LIKE.value}


def test_physiology_alone_cannot_become_the_dominant_estimated_hypothesis() -> None:
    """A camera can never be the sole basis of an estimate (rule_scorer.evaluate).

    Here behaviour makes only flow eligible; the support rule abstains.  The
    support hypothesis must not come out of the evaluation as the estimated
    dominant state on camera evidence alone.
    """

    scorer = _scorer()
    behaviour = _behaviour_only(WEAK_FLOW_SUPPORT_INELIGIBLE)
    assert behaviour.status == EstimateStatus.ESTIMATED
    assert behaviour.scores.dominant()[0] == SupportState.FLOW_LIKE
    assert behaviour.scores.support_likely == 0.0

    fused = scorer.evaluate(_vector({**WEAK_FLOW_SUPPORT_INELIGIBLE, **STRAINED_CAMERA}))
    dominant, strength = fused.scores.dominant()
    assert not (
        fused.status == EstimateStatus.ESTIMATED and dominant == SupportState.SUPPORT_LIKELY
    ), (
        f"support_likely={fused.scores.support_likely:.3f} became the estimated dominant "
        f"hypothesis (strength {strength:.3f}) although its behaviour rule was ineligible"
    )


@pytest.mark.parametrize(
    ("physiology_weight", "entry", "exit_", "expected"),
    [
        (0.5, 0.40, 0.25, 0.36),
        (0.5, 0.30, 0.20, 0.27),
        (0.2, 0.40, 0.25, 0.20),
        (0.35, 0.40, 0.25, 0.35),
    ],
)
def test_effective_weight_is_capped_below_the_entry_threshold(
    physiology_weight: float, entry: float, exit_: float, expected: float
) -> None:
    config = StateConfig(
        physiology_weight=physiology_weight,
        estimate_entry_threshold=entry,
        estimate_exit_threshold=exit_,
    )
    scorer = _scorer(config)
    assert scorer.physiology_weight == pytest.approx(expected)
    assert scorer.physiology_weight <= 0.9 * entry + 1e-12
    # The cap is the weight actually used in fusion, not only a reported value.
    b = RuleScorer(config=config).evaluate(_vector(MODERATE_SUPPORT)).scores.support_likely
    raised = scorer.evaluate(_vector({**MODERATE_SUPPORT, **STRAINED_CAMERA}))
    assert raised.scores.support_likely == pytest.approx((1.0 - expected) * b + expected)


def test_last_physiology_is_recorded_even_when_behaviour_is_ineligible() -> None:
    scorer = _scorer()
    evaluation = scorer.evaluate(_vector(STRAINED_CAMERA))
    assert evaluation.status == EstimateStatus.INSUFFICIENT_EVIDENCE
    block = scorer.last_physiology.support_likely
    assert block.coverage == pytest.approx(1.0)
    assert block.score == pytest.approx(1.0)
    # ...but it is not presented as evidence for an estimate that was not made.
    physiology_names = {name.value for name in STRAINED_CAMERA}
    assert not physiology_names & {item.feature for item in evaluation.contributing_features}


def _physiology_rows(evaluation: RuleEvaluation) -> list[FeatureContribution]:
    names = {name.value for name in STRAINED_CAMERA}
    return [item for item in evaluation.contributing_features if item.feature in names]


def test_contributions_list_the_physiology_features_with_a_note() -> None:
    scorer = _scorer()
    evaluation = scorer.evaluate(_vector({**MODERATE_SUPPORT, **STRAINED_CAMERA}))
    assert evaluation.scores.under_engaged == 0.0
    rows = _physiology_rows(evaluation)
    support_rows = [r for r in rows if r.support_state == SupportState.SUPPORT_LIKELY.value]
    assert {r.feature for r in support_rows} == {name.value for name in SUPPORT_WEIGHTS}
    assert all(r.direction == "positive" for r in support_rows)
    assert all(r.observed for r in rows)
    assert all("personal reference" in r.note for r in rows)
    # Rows appear only for states that were fused, so the under-engaged-only
    # eye-closure input is absent while that rule is not eligible.
    positive = {
        state.value
        for state in (SupportState.SUPPORT_LIKELY, SupportState.FLOW_LIKE, SupportState.UNDER_ENGAGED)
        if getattr(evaluation.scores, state.value) > 0.0
    }
    assert {r.support_state for r in rows} <= positive
    assert FeatureName.PERCLOS.value not in {r.feature for r in rows}


def test_eye_closure_is_listed_when_the_under_engaged_rule_is_fused() -> None:
    scorer = _scorer()
    evaluation = scorer.evaluate(_vector({**QUIET, **STRAINED_CAMERA}))
    assert evaluation.scores.under_engaged > 0.0
    under_rows = [
        r
        for r in _physiology_rows(evaluation)
        if r.support_state == SupportState.UNDER_ENGAGED.value
    ]
    assert {r.feature for r in under_rows} == {name.value for name in UNDER_ENGAGED_WEIGHTS}


def test_zero_physiology_weight_reproduces_behaviour_only_output_exactly() -> None:
    scorer = _scorer(StateConfig(physiology_weight=0.0))
    for behaviour in (MODERATE_SUPPORT, STEADY_FLOW, QUIET):
        for camera in (STRAINED_CAMERA, CALM_CAMERA):
            fused = scorer.evaluate(_vector({**behaviour, **camera}))
            assert fused.model_dump() == _behaviour_only(behaviour).model_dump()
            assert scorer.last_physiology is NO_PHYSIOLOGY


def test_clearing_the_reference_returns_to_behaviour_only() -> None:
    scorer = _scorer()
    vector = _vector({**MODERATE_SUPPORT, **STRAINED_CAMERA})
    assert scorer.evaluate(vector).scores != _behaviour_only(MODERATE_SUPPORT).scores
    scorer.set_physiology_reference(None)
    assert scorer.evaluate(vector).model_dump() == _behaviour_only(MODERATE_SUPPORT).model_dump()


def test_fused_coverage_never_falls_below_the_behaviour_share() -> None:
    scorer = _scorer()
    weight = scorer.physiology_weight
    behaviour = _behaviour_only(MODERATE_SUPPORT)
    fused = scorer.evaluate(_vector({**MODERATE_SUPPORT, **CALM_CAMERA}))
    for state in (SupportState.SUPPORT_LIKELY, SupportState.FLOW_LIKE, SupportState.UNDER_ENGAGED):
        before = behaviour.state_coverage[state]
        after = fused.state_coverage[state]
        assert after >= (1.0 - weight) * before - 1e-9
        assert after >= before - 1e-9  # a fully observed block only adds coverage
