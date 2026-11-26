"""Physiology evidence block for the support rules.

Behaviour (keyboard, pointer, focus) tells the rules *what the person is
doing*; camera physiology adds *how the body is responding while they do it*.
The two are fused so that physiology can strengthen a behavioural reading it
agrees with and temper one it contradicts, but can never establish a support
state by itself:

    fused[s] = (1 - L * C[s]) * behaviour[s] + L * P[s]

``P[s]`` is this block's score for state ``s``, ``C[s]`` its quality-weighted
coverage (``P[s] <= C[s]``) and ``L`` the configured physiology weight.

These consequences follow and are tested:

* with no camera, or no ready personal reference, ``C[s] == 0`` and the fused
  score *is* the behaviour score — nothing changes for a user who never
  enables the camera;
* physiology moves the score only inside
  ``[(1 - L) * behaviour, (1 - L) * behaviour + L]``;
* the scorer fuses a state only where that state's own behaviour rule
  produced a positive eligible score; an abstaining or gated rule leaves the
  state at zero whatever the camera reads;
* the scorer lets physiology raise a score only where behaviour alone is
  already past the smoother's entry threshold; below it physiology can only
  lower the score.  A weak or ambiguous behavioural reading therefore cannot
  be turned into an estimate by the camera.

Every transform standardises against the person's own reference
(:mod:`physiology_baseline`), so a constant instrument bias does not read as
strain.  The directions follow well-replicated group-level findings — mental
load raises heart rate and breathing rate and lowers heart-rate
variability, sustained visual attention suppresses blinking, drowsiness
lengthens eye closure — which is what makes them usable as corroboration and
is also all they are: none of them is a measurement of stress in a person.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from cortex.libs.schemas.features import FeatureName, FeatureValue
from cortex.libs.schemas.state import FeatureContribution, SupportState
from cortex.services.state_engine.physiology_baseline import PhysiologyReference

PHYSIOLOGY_FEATURES: tuple[FeatureName, ...] = (
    FeatureName.HEART_RATE_BPM,
    FeatureName.HRV_SDNN_MS,
    FeatureName.RESPIRATION_RATE_BPM,
    FeatureName.BLINK_RATE_PER_MIN,
    FeatureName.PERCLOS,
)

# Heart rate and dispersion (SDNN) agreed with ECG and carry the support and
# flow blocks. Breathing rate is agreement-gated but has no reference-sensor
# check behind it, and blinking is an attention proxy rather than a strain
# measure, so both weigh less there. The under-engaged block is different:
# eye-closure share is its largest single weight (0.40) because prolonged
# eye closure is what that hypothesis is about -- and it, like blink rate,
# only joins while a cardiac measurement is live (see ``_standard_scores``).
SUPPORT_WEIGHTS: dict[FeatureName, float] = {
    FeatureName.HEART_RATE_BPM: 0.35,
    FeatureName.HRV_SDNN_MS: 0.35,
    FeatureName.RESPIRATION_RATE_BPM: 0.15,
    FeatureName.BLINK_RATE_PER_MIN: 0.15,
}
FLOW_WEIGHTS: dict[FeatureName, float] = {
    FeatureName.HEART_RATE_BPM: 0.30,
    FeatureName.HRV_SDNN_MS: 0.30,
    FeatureName.RESPIRATION_RATE_BPM: 0.15,
    FeatureName.BLINK_RATE_PER_MIN: 0.25,
}
UNDER_ENGAGED_WEIGHTS: dict[FeatureName, float] = {
    FeatureName.HEART_RATE_BPM: 0.25,
    FeatureName.PERCLOS: 0.40,
    FeatureName.BLINK_RATE_PER_MIN: 0.20,
    FeatureName.RESPIRATION_RATE_BPM: 0.15,
}

for _name, _weights in (
    ("support_likely", SUPPORT_WEIGHTS),
    ("flow_like", FLOW_WEIGHTS),
    ("under_engaged", UNDER_ENGAGED_WEIGHTS),
):
    if not np.isclose(sum(_weights.values()), 1.0):
        raise RuntimeError(f"physiology {_name} weights must sum to 1.0")


def _ramp(value: float, low: float, high: float) -> float:
    return float(np.clip((value - low) / (high - low), 0.0, 1.0))


@dataclass(frozen=True, slots=True)
class PhysiologyBlock:
    """Score and coverage of the physiology block for one state."""

    score: float
    coverage: float
    observed_count: int


@dataclass(frozen=True, slots=True)
class PhysiologyEvidence:
    """The physiology block for all three stateless hypotheses."""

    support_likely: PhysiologyBlock
    flow_like: PhysiologyBlock
    under_engaged: PhysiologyBlock
    contributions: tuple[FeatureContribution, ...]

    @property
    def available(self) -> bool:
        return any(
            block.coverage > 0.0
            for block in (self.support_likely, self.flow_like, self.under_engaged)
        )

    def block(self, state: SupportState) -> PhysiologyBlock:
        return {
            SupportState.SUPPORT_LIKELY: self.support_likely,
            SupportState.FLOW_LIKE: self.flow_like,
            SupportState.UNDER_ENGAGED: self.under_engaged,
        }[state]


_EMPTY_BLOCK = PhysiologyBlock(score=0.0, coverage=0.0, observed_count=0)
NO_PHYSIOLOGY = PhysiologyEvidence(
    support_likely=_EMPTY_BLOCK,
    flow_like=_EMPTY_BLOCK,
    under_engaged=_EMPTY_BLOCK,
    contributions=(),
)


def fuse(behaviour: float, block: PhysiologyBlock, weight: float) -> float:
    """Fused score for one state (see the module docstring)."""

    return float(
        np.clip(
            (1.0 - weight * block.coverage) * behaviour + weight * block.score,
            0.0,
            1.0,
        )
    )


def fuse_coverage(behaviour_coverage: float, block: PhysiologyBlock, weight: float) -> float:
    """Evidence coverage of the fused score: the same mixture of coverages."""

    return float(
        np.clip(
            (1.0 - weight * block.coverage) * behaviour_coverage
            + weight * block.coverage,
            0.0,
            1.0,
        )
    )


def _standard_scores(
    features: dict[FeatureName, FeatureValue],
    reference: PhysiologyReference,
) -> dict[FeatureName, tuple[float, float]]:
    """Per-feature (standardised value, quality) for every usable input.

    Heart rate, variability and breathing are z-scores against the personal
    reference; blink rate is the ratio to its reference (blinking is a count
    process, and "a third of normal" means the same at any baseline);
    eye-closure share is already an absolute fraction, read on a fixed scale.
    The two eye inputs are returned only alongside a usable heart rate or
    SDNN.
    """

    usable: dict[FeatureName, tuple[float, float]] = {}

    def _value(name: FeatureName) -> FeatureValue | None:
        item = features.get(name)
        if item is None or not item.valid or item.value is None:
            return None
        return item

    for name, metric_reference in (
        (FeatureName.HEART_RATE_BPM, reference.heart_rate),
        (FeatureName.HRV_SDNN_MS, reference.sdnn),
        (FeatureName.RESPIRATION_RATE_BPM, reference.respiration),
    ):
        item = _value(name)
        if item is not None and item.value is not None and metric_reference.ready:
            usable[name] = (metric_reference.z(float(item.value)), float(item.quality))

    # Blink rate and eye-closure share were not checked against any reference
    # sensor. They ride along with the measurements that were: neither joins
    # the block unless heart rate or SDNN is usable in this same evaluation.
    # Where the camera yields no pulse -- the two darker-skinned participants
    # of the qualification, poor light, frequent movement -- the block is
    # therefore empty and the rules are behaviour-only, as documented.
    cardiac_live = (
        FeatureName.HEART_RATE_BPM in usable or FeatureName.HRV_SDNN_MS in usable
    )
    if not cardiac_live:
        return usable

    blink = _value(FeatureName.BLINK_RATE_PER_MIN)
    if (
        blink is not None
        and blink.value is not None
        and reference.blink_rate.ready
        and reference.blink_rate.center > 1.0
    ):
        usable[FeatureName.BLINK_RATE_PER_MIN] = (
            float(blink.value) / reference.blink_rate.center,
            float(blink.quality),
        )

    # Eye-closure share is an absolute fraction and needs no reference of its
    # own.
    perclos = _value(FeatureName.PERCLOS)
    if perclos is not None and perclos.value is not None:
        usable[FeatureName.PERCLOS] = (float(perclos.value), float(perclos.quality))
    return usable


def _support_transform(name: FeatureName, x: float) -> float:
    if name is FeatureName.HEART_RATE_BPM:
        return _ramp(x, 1.0, 3.0)
    if name is FeatureName.HRV_SDNN_MS:
        return _ramp(-x, 1.0, 2.5)
    if name is FeatureName.RESPIRATION_RATE_BPM:
        return _ramp(x, 1.0, 3.0)
    if name is FeatureName.BLINK_RATE_PER_MIN:
        # Blinking at 75% of one's norm is unremarkable; at 35% it is
        # markedly suppressed.
        return 1.0 - _ramp(x, 0.35, 0.75)
    return 0.0


def _flow_transform(name: FeatureName, x: float) -> float:
    if name is FeatureName.HEART_RATE_BPM:
        return 1.0 - _ramp(abs(x), 0.75, 2.0)
    if name is FeatureName.HRV_SDNN_MS:
        return 1.0 - _ramp(-x, 0.5, 2.0)
    if name is FeatureName.RESPIRATION_RATE_BPM:
        return 1.0 - _ramp(abs(x), 0.75, 2.0)
    if name is FeatureName.BLINK_RATE_PER_MIN:
        if x < 0.45:
            return _ramp(x, 0.20, 0.45)
        return 1.0 - _ramp(x, 1.10, 1.60)
    return 0.0


def _under_engaged_transform(name: FeatureName, x: float) -> float:
    if name is FeatureName.HEART_RATE_BPM:
        return _ramp(-x, 1.0, 2.5)
    if name is FeatureName.PERCLOS:
        return _ramp(x, 0.15, 0.40)
    if name is FeatureName.BLINK_RATE_PER_MIN:
        return _ramp(x, 1.40, 2.20)
    if name is FeatureName.RESPIRATION_RATE_BPM:
        return _ramp(-x, 1.0, 2.5)
    return 0.0


def evaluate_physiology(
    features: dict[FeatureName, FeatureValue],
    reference: PhysiologyReference | None,
) -> PhysiologyEvidence:
    """Score the physiology block from named features and a personal reference."""

    if reference is None:
        return NO_PHYSIOLOGY
    usable = _standard_scores(features, reference)
    if not usable:
        return NO_PHYSIOLOGY

    contributions: list[FeatureContribution] = []

    def _block(
        state: SupportState,
        weights: dict[FeatureName, float],
        transform: Callable[[FeatureName, float], float],
    ) -> PhysiologyBlock:
        score = 0.0
        coverage = 0.0
        observed = 0
        for name, weight in weights.items():
            entry = usable.get(name)
            if entry is None:
                contributions.append(
                    FeatureContribution(
                        feature=name.value,
                        support_state=state,
                        direction="missing",
                        contribution=0.0,
                        quality=0.0,
                        observed=False,
                        note=(
                            "Camera measurement or its personal reference is "
                            "not available; it contributes neither evidence "
                            "nor score."
                        ),
                    )
                )
                continue
            standardised, quality = entry
            transformed = float(np.clip(transform(name, standardised), 0.0, 1.0))
            observed += 1
            score += weight * quality * transformed
            coverage += weight * quality
            contributions.append(
                FeatureContribution(
                    feature=name.value,
                    support_state=state,
                    direction="positive" if transformed >= 0.5 else "negative",
                    contribution=(
                        weight * quality * transformed
                        if transformed >= 0.5
                        else -weight * quality * (1.0 - transformed)
                    ),
                    quality=quality,
                    observed=True,
                    note=(
                        f"Physiology weight {weight:.2f}; relative to personal "
                        f"reference={standardised:+.2f}; transform={transformed:.3f}."
                    ),
                )
            )
        return PhysiologyBlock(
            score=float(np.clip(score, 0.0, 1.0)),
            coverage=float(np.clip(coverage, 0.0, 1.0)),
            observed_count=observed,
        )

    return PhysiologyEvidence(
        support_likely=_block(
            SupportState.SUPPORT_LIKELY, SUPPORT_WEIGHTS, _support_transform
        ),
        flow_like=_block(SupportState.FLOW_LIKE, FLOW_WEIGHTS, _flow_transform),
        under_engaged=_block(
            SupportState.UNDER_ENGAGED, UNDER_ENGAGED_WEIGHTS, _under_engaged_transform
        ),
        contributions=tuple(contributions),
    )
