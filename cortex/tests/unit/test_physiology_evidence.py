"""Physiology evidence block: fusion bounds, transform directions, quality weighting.

The block may strengthen or temper a behavioural reading but never establish
one by itself; these tests pin the bounds that make that true and the
direction each camera measurement pushes each hypothesis.
"""

from __future__ import annotations

from itertools import product

import numpy as np
import pytest

from cortex.libs.schemas.features import FeatureName, FeatureValue
from cortex.libs.schemas.observations import MissingReason
from cortex.libs.schemas.state import SupportState
from cortex.services.state_engine.feature_schema import FEATURE_DEFINITIONS
from cortex.services.state_engine.physiology_baseline import (
    MetricReference,
    PhysiologyReference,
)
from cortex.services.state_engine.physiology_evidence import (
    FLOW_WEIGHTS,
    NO_PHYSIOLOGY,
    PHYSIOLOGY_FEATURES,
    SUPPORT_WEIGHTS,
    UNDER_ENGAGED_WEIGHTS,
    PhysiologyBlock,
    PhysiologyEvidence,
    evaluate_physiology,
    fuse,
    fuse_coverage,
)

HR_CENTER, HR_SPREAD = 70.0, 5.0
SDNN_CENTER, SDNN_SPREAD = 50.0, 10.0
RESP_CENTER, RESP_SPREAD = 14.0, 2.0
BLINK_CENTER, BLINK_SPREAD = 17.0, 4.0

WEIGHT_TABLES: dict[SupportState, dict[FeatureName, float]] = {
    SupportState.SUPPORT_LIKELY: SUPPORT_WEIGHTS,
    SupportState.FLOW_LIKE: FLOW_WEIGHTS,
    SupportState.UNDER_ENGAGED: UNDER_ENGAGED_WEIGHTS,
}


def _ready(center: float, spread: float) -> MetricReference:
    return MetricReference(center=center, spread=spread, source="session", ready=True)


READY = PhysiologyReference(
    heart_rate=_ready(HR_CENTER, HR_SPREAD),
    sdnn=_ready(SDNN_CENTER, SDNN_SPREAD),
    respiration=_ready(RESP_CENTER, RESP_SPREAD),
    blink_rate=_ready(BLINK_CENTER, BLINK_SPREAD),
)


def _value(name: FeatureName, value: float, quality: float = 1.0) -> FeatureValue:
    return FeatureValue(
        value=value,
        valid=True,
        quality=quality,
        age_ms=0,
        source_window_ms=FEATURE_DEFINITIONS[name].source_window_ms,
        algorithm_version="test-v1",
    )


def _invalid(name: FeatureName) -> FeatureValue:
    return FeatureValue(
        valid=False,
        quality=0.0,
        age_ms=0,
        source_window_ms=FEATURE_DEFINITIONS[name].source_window_ms,
        algorithm_version="test-v1",
        missing_reason=MissingReason.NO_FACE,
    )


def _features(
    values: dict[FeatureName, float], quality: float = 1.0
) -> dict[FeatureName, FeatureValue]:
    return {name: _value(name, value, quality) for name, value in values.items()}


# Heart rate well above the reference, SDNN well below it, fast breathing and
# suppressed blinking: every support transform saturates.
STRAINED: dict[FeatureName, float] = {
    FeatureName.HEART_RATE_BPM: HR_CENTER + 3.0 * HR_SPREAD,
    FeatureName.HRV_SDNN_MS: SDNN_CENTER - 2.5 * SDNN_SPREAD,
    FeatureName.RESPIRATION_RATE_BPM: RESP_CENTER + 3.0 * RESP_SPREAD,
    FeatureName.BLINK_RATE_PER_MIN: 0.30 * BLINK_CENTER,
    FeatureName.PERCLOS: 0.05,
}
# Everything at the personal reference.
AT_REFERENCE: dict[FeatureName, float] = {
    FeatureName.HEART_RATE_BPM: HR_CENTER,
    FeatureName.HRV_SDNN_MS: SDNN_CENTER,
    FeatureName.RESPIRATION_RATE_BPM: RESP_CENTER,
    FeatureName.BLINK_RATE_PER_MIN: BLINK_CENTER,
    FeatureName.PERCLOS: 0.05,
}
# Long eye closure, low heart rate, slow breathing, frequent blinking.
DROWSY: dict[FeatureName, float] = {
    FeatureName.HEART_RATE_BPM: HR_CENTER - 2.5 * HR_SPREAD,
    FeatureName.HRV_SDNN_MS: SDNN_CENTER,
    FeatureName.RESPIRATION_RATE_BPM: RESP_CENTER - 2.5 * RESP_SPREAD,
    FeatureName.BLINK_RATE_PER_MIN: 2.2 * BLINK_CENTER,
    FeatureName.PERCLOS: 0.45,
}


def _ratio(block: PhysiologyBlock) -> float:
    return block.score / block.coverage if block.coverage > 0 else 0.0


# ---------------------------------------------------------------------------
# Absent camera / absent reference
# ---------------------------------------------------------------------------


def test_no_reference_or_no_usable_features_is_no_physiology() -> None:
    """Without a reference or a usable measurement the block is empty."""

    assert evaluate_physiology(_features(STRAINED), None) is NO_PHYSIOLOGY
    assert evaluate_physiology({}, READY) is NO_PHYSIOLOGY
    invalid = {name: _invalid(name) for name in PHYSIOLOGY_FEATURES}
    assert evaluate_physiology(invalid, READY) is NO_PHYSIOLOGY
    assert not NO_PHYSIOLOGY.available
    for state in WEIGHT_TABLES:
        block = NO_PHYSIOLOGY.block(state)
        assert block.score == block.coverage == 0.0
        assert block.observed_count == 0


def test_reference_dependent_metrics_abstain_until_their_reference_is_ready() -> None:
    """Heart rate, SDNN, breathing and blink rate are read only against a ready reference."""

    without_perclos = {k: v for k, v in STRAINED.items() if k is not FeatureName.PERCLOS}
    assert evaluate_physiology(_features(without_perclos), PhysiologyReference()) is NO_PHYSIOLOGY


def test_no_ready_reference_leaves_every_block_without_coverage() -> None:
    """Module docstring: "with no camera, or no ready personal reference, C[s] == 0".

    The daemon always hands the scorer a reference object; during warm-up it
    is simply not ready.  A camera that is on during warm-up therefore must
    not move any hypothesis yet.
    """

    evidence = evaluate_physiology(_features(DROWSY), PhysiologyReference())
    for state in WEIGHT_TABLES:
        assert evidence.block(state).coverage == 0.0, (
            f"{state.value} gained coverage {evidence.block(state).coverage} "
            "from a reference that is not ready"
        )


@pytest.mark.parametrize("behaviour", [0.0, 0.3, 1.0])
@pytest.mark.parametrize("weight", [0.0, 0.2, 0.36, 0.5])
def test_empty_block_leaves_behaviour_untouched(behaviour: float, weight: float) -> None:
    """With zero coverage the fused score *is* the behaviour score."""

    for state in WEIGHT_TABLES:
        block = NO_PHYSIOLOGY.block(state)
        assert fuse(behaviour, block, weight) == behaviour
        assert fuse_coverage(behaviour, block, weight) == behaviour


# ---------------------------------------------------------------------------
# Fusion bounds
# ---------------------------------------------------------------------------


_GRID = np.linspace(0.0, 1.0, 11)
_WEIGHTS = (0.0, 0.1, 0.27, 0.36, 0.5)


def test_fused_score_stays_inside_documented_bounds() -> None:
    """fused in [(1-w)b, (1-w)b + w] for every block with 0 <= P <= C <= 1."""

    for behaviour, coverage, weight in product(_GRID, _GRID, _WEIGHTS):
        for score in _GRID[_GRID <= coverage + 1e-12]:
            block = PhysiologyBlock(score=float(score), coverage=float(coverage), observed_count=1)
            fused = fuse(float(behaviour), block, weight)
            assert (1.0 - weight) * behaviour - 1e-12 <= fused
            assert fused <= (1.0 - weight) * behaviour + weight + 1e-12
            if behaviour == 0.0:
                assert fused <= weight + 1e-12


def test_evaluated_blocks_never_score_above_their_coverage() -> None:
    """P <= C is what the fusion bounds rest on; check it on real evaluations."""

    rng = np.random.default_rng(20261125)
    for _ in range(300):
        values = {
            FeatureName.HEART_RATE_BPM: float(rng.uniform(40.0, 140.0)),
            FeatureName.HRV_SDNN_MS: float(rng.uniform(5.0, 150.0)),
            FeatureName.RESPIRATION_RATE_BPM: float(rng.uniform(5.0, 35.0)),
            FeatureName.BLINK_RATE_PER_MIN: float(rng.uniform(0.0, 50.0)),
            FeatureName.PERCLOS: float(rng.uniform(0.0, 1.0)),
        }
        present = {name: v for name, v in values.items() if rng.random() < 0.8}
        features = {
            name: _value(name, value, float(rng.uniform(0.0, 1.0)))
            for name, value in present.items()
        }
        evidence = evaluate_physiology(features, READY)
        for state in WEIGHT_TABLES:
            block = evidence.block(state)
            assert 0.0 <= block.score <= block.coverage + 1e-12 <= 1.0 + 1e-12


def test_fuse_coverage_is_the_same_mixture_as_the_score() -> None:
    """Coverage fuses like a score whose physiology part is fully observed (P = C)."""

    for behaviour_coverage, coverage, weight in product(_GRID, _GRID, _WEIGHTS):
        block = PhysiologyBlock(score=0.0, coverage=float(coverage), observed_count=1)
        as_score = PhysiologyBlock(
            score=float(coverage), coverage=float(coverage), observed_count=1
        )
        assert fuse_coverage(float(behaviour_coverage), block, weight) == pytest.approx(
            fuse(float(behaviour_coverage), as_score, weight)
        )
        # Physiology coverage can only add to what behaviour observed.
        assert fuse_coverage(float(behaviour_coverage), block, weight) >= (
            (1.0 - weight) * behaviour_coverage - 1e-12
        )


# ---------------------------------------------------------------------------
# Transform directions
# ---------------------------------------------------------------------------


def test_strained_physiology_scores_support_likely() -> None:
    evidence = evaluate_physiology(_features(STRAINED), READY)
    support = evidence.support_likely
    assert support.coverage == pytest.approx(1.0)
    assert support.score == pytest.approx(1.0)
    assert _ratio(evidence.flow_like) < 0.25


def test_physiology_at_reference_scores_flow_like_not_support() -> None:
    evidence = evaluate_physiology(_features(AT_REFERENCE), READY)
    assert evidence.support_likely.score == pytest.approx(0.0)
    assert evidence.flow_like.score == pytest.approx(evidence.flow_like.coverage)
    assert evidence.flow_like.coverage == pytest.approx(1.0)
    assert evidence.under_engaged.score == pytest.approx(0.0)


def test_drowsy_physiology_scores_under_engaged() -> None:
    evidence = evaluate_physiology(_features(DROWSY), READY)
    assert evidence.under_engaged.score == pytest.approx(1.0)
    assert evidence.support_likely.score < 0.2


@pytest.mark.parametrize(
    ("name", "pushed", "neutral"),
    [
        (FeatureName.HEART_RATE_BPM, HR_CENTER + 3.0 * HR_SPREAD, HR_CENTER),
        (FeatureName.HRV_SDNN_MS, SDNN_CENTER - 2.5 * SDNN_SPREAD, SDNN_CENTER),
        (FeatureName.RESPIRATION_RATE_BPM, RESP_CENTER + 3.0 * RESP_SPREAD, RESP_CENTER),
        (FeatureName.BLINK_RATE_PER_MIN, 0.30 * BLINK_CENTER, BLINK_CENTER),
    ],
)
def test_each_strain_marker_pushes_support_and_its_absence_pushes_flow(
    name: FeatureName, pushed: float, neutral: float
) -> None:
    """Each strain marker saturates its share of support; at reference it reads as flow.

    Blink rate only joins the block beside a live cardiac measurement, so it
    is tested with a heart rate sitting exactly at its reference (which adds
    coverage and flow, and no support).
    """

    companion = _eye_companion(name)
    companion_support = sum(SUPPORT_WEIGHTS[item] for item in companion)
    companion_flow = sum(FLOW_WEIGHTS[item] for item in companion)

    strained = evaluate_physiology({**companion, name: _value(name, pushed)}, READY)
    assert strained.support_likely.score == pytest.approx(SUPPORT_WEIGHTS[name])
    assert strained.support_likely.coverage == pytest.approx(
        SUPPORT_WEIGHTS[name] + companion_support
    )

    calm = evaluate_physiology({**companion, name: _value(name, neutral)}, READY)
    assert calm.support_likely.score == pytest.approx(0.0)
    assert calm.flow_like.score == pytest.approx(FLOW_WEIGHTS[name] + companion_flow)


@pytest.mark.parametrize(
    ("name", "pushed", "neutral"),
    [
        (FeatureName.PERCLOS, 0.40, 0.10),
        (FeatureName.HEART_RATE_BPM, HR_CENTER - 2.5 * HR_SPREAD, HR_CENTER),
        (FeatureName.BLINK_RATE_PER_MIN, 2.2 * BLINK_CENTER, BLINK_CENTER),
        (FeatureName.RESPIRATION_RATE_BPM, RESP_CENTER - 2.5 * RESP_SPREAD, RESP_CENTER),
    ],
)
def test_each_drowsiness_marker_pushes_under_engaged(
    name: FeatureName, pushed: float, neutral: float
) -> None:
    # The eye inputs are tested beside a heart rate at its reference, which
    # adds coverage and no under-engaged score.
    companion = _eye_companion(name)
    pushed_block = evaluate_physiology(
        {**companion, name: _value(name, pushed)}, READY
    ).under_engaged
    neutral_block = evaluate_physiology(
        {**companion, name: _value(name, neutral)}, READY
    ).under_engaged
    assert pushed_block.score == pytest.approx(UNDER_ENGAGED_WEIGHTS[name])
    assert neutral_block.score == pytest.approx(0.0)
    assert neutral_block.coverage == pytest.approx(pushed_block.coverage)


def _eye_companion(name: FeatureName) -> dict[FeatureName, FeatureValue]:
    """A heart rate at its reference, for the inputs that need a live cardiac one."""

    if name in (FeatureName.BLINK_RATE_PER_MIN, FeatureName.PERCLOS):
        return {FeatureName.HEART_RATE_BPM: _value(FeatureName.HEART_RATE_BPM, HR_CENTER)}
    return {}


@pytest.mark.parametrize(
    "eye_only",
    [
        {FeatureName.BLINK_RATE_PER_MIN: 0.30 * BLINK_CENTER},
        {FeatureName.BLINK_RATE_PER_MIN: 2.2 * BLINK_CENTER},
        {FeatureName.PERCLOS: 0.40},
        {FeatureName.BLINK_RATE_PER_MIN: 2.2 * BLINK_CENTER, FeatureName.PERCLOS: 0.40},
        # Breathing is not a cardiac measurement for this purpose either.
        {
            FeatureName.PERCLOS: 0.40,
            FeatureName.BLINK_RATE_PER_MIN: 2.2 * BLINK_CENTER,
        },
    ],
)
def test_eye_inputs_without_a_live_pulse_contribute_nothing(
    eye_only: dict[FeatureName, float],
) -> None:
    """No pulse, no physiology block: a user for whom the camera yields no
    heart rate or SDNN gets behaviour-only rules, even with every personal
    reference ready and the eye inputs at their extremes."""

    evidence = evaluate_physiology(_features(eye_only), READY)
    assert evidence.available is False
    for block in (evidence.support_likely, evidence.flow_like, evidence.under_engaged):
        assert block.score == 0.0 and block.coverage == 0.0 and block.observed_count == 0


def test_eye_inputs_join_once_heart_rate_or_sdnn_is_live() -> None:
    eyes = {FeatureName.BLINK_RATE_PER_MIN: 2.2 * BLINK_CENTER, FeatureName.PERCLOS: 0.40}
    for cardiac in (
        {FeatureName.HEART_RATE_BPM: HR_CENTER},
        {FeatureName.HRV_SDNN_MS: SDNN_CENTER},
    ):
        evidence = evaluate_physiology(_features({**eyes, **cardiac}), READY)
        expected = UNDER_ENGAGED_WEIGHTS[FeatureName.PERCLOS] + UNDER_ENGAGED_WEIGHTS[
            FeatureName.BLINK_RATE_PER_MIN
        ]
        assert evidence.under_engaged.score == pytest.approx(expected)
    # A heart-rate value whose own reference is not ready does not count.
    no_heart_reference = PhysiologyReference(blink_rate=READY.blink_rate)
    evidence = evaluate_physiology(
        _features({**eyes, FeatureName.HEART_RATE_BPM: HR_CENTER}), no_heart_reference
    )
    assert evidence.available is False


def test_constant_offset_in_the_reference_frame_cancels() -> None:
    """A uniform instrument bias in both reference and reading does not read as strain."""

    biased = PhysiologyReference(
        heart_rate=_ready(HR_CENTER + 8.0, HR_SPREAD),
        sdnn=_ready(SDNN_CENTER + 15.0, SDNN_SPREAD),
        respiration=_ready(RESP_CENTER + 2.0, RESP_SPREAD),
        blink_rate=_ready(BLINK_CENTER, BLINK_SPREAD),
    )
    shifted = dict(AT_REFERENCE)
    shifted[FeatureName.HEART_RATE_BPM] += 8.0
    shifted[FeatureName.HRV_SDNN_MS] += 15.0
    shifted[FeatureName.RESPIRATION_RATE_BPM] += 2.0
    assert evaluate_physiology(_features(shifted), biased) == evaluate_physiology(
        _features(AT_REFERENCE), READY
    )


# ---------------------------------------------------------------------------
# Quality and missingness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", list(SUPPORT_WEIGHTS))
def test_halving_quality_halves_contribution_to_score_and_coverage(name: FeatureName) -> None:
    full = _features(STRAINED)
    halved = dict(full)
    halved[name] = _value(name, STRAINED[name], quality=0.5)
    full_block = evaluate_physiology(full, READY).support_likely
    halved_block = evaluate_physiology(halved, READY).support_likely
    weight = SUPPORT_WEIGHTS[name]
    # Every strained transform is 1, so the feature's full contribution is its weight.
    assert full_block.score - halved_block.score == pytest.approx(0.5 * weight)
    assert full_block.coverage - halved_block.coverage == pytest.approx(0.5 * weight)

    alone_full = evaluate_physiology({name: full[name]}, READY).support_likely
    alone_half = evaluate_physiology({name: halved[name]}, READY).support_likely
    assert alone_half.score == pytest.approx(0.5 * alone_full.score)
    assert alone_half.coverage == pytest.approx(0.5 * alone_full.coverage)


@pytest.mark.parametrize("values", [STRAINED, AT_REFERENCE, DROWSY], ids=["strained", "at-reference", "drowsy"])
@pytest.mark.parametrize("missing", list(PHYSIOLOGY_FEATURES))
def test_missing_metric_lowers_coverage_and_never_raises_score(
    values: dict[FeatureName, float], missing: FeatureName
) -> None:
    full = evaluate_physiology(_features(values), READY)
    reduced_values = {k: v for k, v in values.items() if k is not missing}
    reduced = evaluate_physiology(_features(reduced_values), READY)
    for state, weights in WEIGHT_TABLES.items():
        before, after = full.block(state), reduced.block(state)
        assert after.score <= before.score + 1e-12
        if missing in weights:
            assert after.coverage == pytest.approx(before.coverage - weights[missing])
            assert after.observed_count == before.observed_count - 1
        else:
            assert after == before
    missing_rows = [
        item
        for item in reduced.contributions
        if item.feature == missing.value and item.direction == "missing"
    ]
    assert len(missing_rows) == sum(missing in w for w in WEIGHT_TABLES.values())
    assert all(not item.observed and item.contribution == 0.0 for item in missing_rows)


def test_invalid_measurement_counts_as_missing() -> None:
    features = _features(STRAINED)
    features[FeatureName.HEART_RATE_BPM] = _invalid(FeatureName.HEART_RATE_BPM)
    without = {k: v for k, v in _features(STRAINED).items() if k is not FeatureName.HEART_RATE_BPM}
    assert evaluate_physiology(features, READY) == evaluate_physiology(without, READY)


def test_weight_tables_sum_to_one_and_cover_only_physiology_features() -> None:
    for weights in WEIGHT_TABLES.values():
        assert sum(weights.values()) == pytest.approx(1.0)
        assert set(weights) <= set(PHYSIOLOGY_FEATURES)
        assert all(weight > 0.0 for weight in weights.values())


def test_contributions_name_every_weighted_feature_once_per_state() -> None:
    evidence = evaluate_physiology(_features(STRAINED), READY)
    assert isinstance(evidence, PhysiologyEvidence)
    assert evidence.available
    for state, weights in WEIGHT_TABLES.items():
        rows = [item for item in evidence.contributions if item.support_state == state.value]
        assert sorted(item.feature for item in rows) == sorted(name.value for name in weights)
        assert all(item.observed for item in rows)
        assert all("personal reference" in item.note for item in rows)
