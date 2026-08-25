"""Off-policy evaluation with an explicit target policy and diagnostics.

The estimators refuse deterministic product logs, missing support, incomplete
propensities, or an unnamed target policy.  Estimator agreement is reported as
a sensitivity diagnostic and is never promoted to proof of benefit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from cortex.libs.schemas.policy import PolicyArm, policy_payload_sha256


class OffPolicyEvaluationError(ValueError):
    """The logged/target policy contract cannot identify an OPE value."""


@dataclass(frozen=True, slots=True)
class TargetPolicyDefinition:
    name: str
    version: str
    action_catalog: tuple[PolicyArm, ...]
    probability_rule: str

    def __post_init__(self) -> None:
        if not self.name or not self.version or not self.probability_rule:
            raise OffPolicyEvaluationError("target policy must be fully named and specified")
        if not self.action_catalog or len(self.action_catalog) != len(set(self.action_catalog)):
            raise OffPolicyEvaluationError("target action catalog must be non-empty and unique")

    @property
    def sha256(self) -> str:
        return policy_payload_sha256(
            {
                "name": self.name,
                "version": self.version,
                "action_catalog": list(self.action_catalog),
                "probability_rule": self.probability_rule,
            }
        )


@dataclass(frozen=True, slots=True)
class OPEObservation:
    decision_id: str
    cluster_id: str
    selected_arm: PolicyArm
    reward: float
    behavior_probabilities: dict[PolicyArm, float]
    target_probabilities: dict[PolicyArm, float]
    direct_outcome_estimates: dict[PolicyArm, float]
    supports_ope: bool


@dataclass(frozen=True, slots=True)
class OPEEstimate:
    value: float
    confidence_interval_95: tuple[float, float] | None


@dataclass(frozen=True, slots=True)
class OPEResult:
    target_policy_name: str
    target_policy_version: str
    target_policy_sha256: str
    target_assignments_sha256: str
    evaluation_rows_sha256: str
    observations: int
    clusters: int
    direct_method: OPEEstimate
    ips: OPEEstimate
    snips: OPEEstimate
    doubly_robust: OPEEstimate
    clipped_doubly_robust: OPEEstimate
    switch_doubly_robust: OPEEstimate
    diagnostics: dict[str, float | int | bool]

    def to_dict(self) -> dict[str, Any]:
        def estimate(value: OPEEstimate) -> dict[str, Any]:
            return {
                "value": value.value,
                "confidence_interval_95": list(value.confidence_interval_95)
                if value.confidence_interval_95 is not None
                else None,
            }

        return {
            "schema_version": "ope-result/1.0",
            "target_policy": {
                "name": self.target_policy_name,
                "version": self.target_policy_version,
                "sha256": self.target_policy_sha256,
                "assignments_sha256": self.target_assignments_sha256,
            },
            "evaluation_rows_sha256": self.evaluation_rows_sha256,
            "observations": self.observations,
            "clusters": self.clusters,
            "estimators": {
                "direct_method": estimate(self.direct_method),
                "ips": estimate(self.ips),
                "snips": estimate(self.snips),
                "doubly_robust": estimate(self.doubly_robust),
                "clipped_doubly_robust": estimate(self.clipped_doubly_robust),
                "switch_doubly_robust": estimate(self.switch_doubly_robust),
            },
            "diagnostics": self.diagnostics,
            "interpretation": (
                "Sensitivity analysis under overlap, consistency, correct logged behavior "
                "probabilities, and (for DM/DR) outcome-model assumptions. Estimator "
                "agreement does not establish causality."
            ),
        }


def _validate_distribution(
    distribution: dict[PolicyArm, float],
    catalog: tuple[PolicyArm, ...],
    *,
    label: str,
) -> None:
    if set(distribution) != set(catalog):
        raise OffPolicyEvaluationError(f"{label} distribution does not cover the catalog")
    values = np.asarray(list(distribution.values()), dtype=np.float64)
    if not np.all(np.isfinite(values)) or np.any(values < 0.0):
        raise OffPolicyEvaluationError(f"{label} distribution has invalid probabilities")
    if not np.isclose(float(np.sum(values)), 1.0, atol=1e-9):
        raise OffPolicyEvaluationError(f"{label} distribution does not sum to one")


def _point_estimates(
    observations: list[OPEObservation],
    *,
    clip_weight: float,
    switch_threshold: float,
) -> dict[str, float]:
    weights: list[float] = []
    rewards: list[float] = []
    direct: list[float] = []
    residuals: list[float] = []
    for observation in observations:
        behavior = observation.behavior_probabilities[observation.selected_arm]
        target = observation.target_probabilities[observation.selected_arm]
        weight = target / behavior
        direct_value = sum(
            observation.target_probabilities[arm] * observation.direct_outcome_estimates[arm]
            for arm in observation.target_probabilities
        )
        weights.append(weight)
        rewards.append(observation.reward)
        direct.append(direct_value)
        residuals.append(
            observation.reward - observation.direct_outcome_estimates[observation.selected_arm]
        )
    w = np.asarray(weights, dtype=np.float64)
    y = np.asarray(rewards, dtype=np.float64)
    dm = np.asarray(direct, dtype=np.float64)
    residual = np.asarray(residuals, dtype=np.float64)
    if float(np.sum(w)) <= 0.0:
        raise OffPolicyEvaluationError("target policy has no observed support")
    return {
        "direct_method": float(np.mean(dm)),
        "ips": float(np.mean(w * y)),
        "snips": float(np.sum(w * y) / np.sum(w)),
        "doubly_robust": float(np.mean(dm + w * residual)),
        "clipped_doubly_robust": float(np.mean(dm + np.minimum(w, clip_weight) * residual)),
        "switch_doubly_robust": float(
            np.mean(dm + np.where(w <= switch_threshold, w * residual, 0.0))
        ),
    }


def evaluate_target_policy(
    target_policy: TargetPolicyDefinition,
    observations: list[OPEObservation],
    *,
    clip_weight: float = 20.0,
    switch_threshold: float = 10.0,
    bootstrap_samples: int = 1_000,
    bootstrap_seed: int = 0,
) -> OPEResult:
    if not observations:
        raise OffPolicyEvaluationError("OPE requires at least one complete observation")
    if clip_weight <= 0.0 or switch_threshold <= 0.0:
        raise OffPolicyEvaluationError("weight thresholds must be positive")
    if bootstrap_samples < 0:
        raise OffPolicyEvaluationError("bootstrap_samples cannot be negative")
    catalog = target_policy.action_catalog
    decision_ids: set[str] = set()
    for observation in observations:
        if not observation.decision_id or observation.decision_id in decision_ids:
            raise OffPolicyEvaluationError("OPE decision ids must be non-empty and unique")
        decision_ids.add(observation.decision_id)
        if not observation.cluster_id:
            raise OffPolicyEvaluationError("OPE cluster ids must be non-empty")
        if not observation.supports_ope:
            raise OffPolicyEvaluationError(
                "deterministic or structurally incomplete records cannot enter OPE"
            )
        if observation.selected_arm not in catalog:
            raise OffPolicyEvaluationError("logged action is outside target policy catalog")
        if not np.isfinite(observation.reward) or abs(observation.reward) > 1.0:
            raise OffPolicyEvaluationError("reward is not finite and bounded")
        _validate_distribution(
            observation.behavior_probabilities,
            catalog,
            label="behavior",
        )
        _validate_distribution(
            observation.target_probabilities,
            catalog,
            label="target",
        )
        if observation.behavior_probabilities[observation.selected_arm] <= 0.0:
            raise OffPolicyEvaluationError("observed action has zero behavior probability")
        if set(observation.direct_outcome_estimates) != set(catalog):
            raise OffPolicyEvaluationError("direct outcome model does not cover the catalog")
        if any(
            not np.isfinite(value) or abs(value) > 1.0
            for value in observation.direct_outcome_estimates.values()
        ):
            raise OffPolicyEvaluationError("direct outcome estimate is invalid")
        for arm in catalog:
            if (
                observation.target_probabilities[arm] > 0.0
                and observation.behavior_probabilities[arm] <= 0.0
            ):
                raise OffPolicyEvaluationError("target policy violates behavior-policy support")

    points = _point_estimates(
        observations,
        clip_weight=clip_weight,
        switch_threshold=switch_threshold,
    )
    clusters = sorted({observation.cluster_id for observation in observations})
    cluster_rows = {
        cluster: [item for item in observations if item.cluster_id == cluster]
        for cluster in clusters
    }
    bootstrap: dict[str, list[float]] = {key: [] for key in points}
    if len(clusters) >= 2 and bootstrap_samples > 0:
        rng = np.random.default_rng(bootstrap_seed)
        for _ in range(bootstrap_samples):
            sampled_clusters = rng.choice(clusters, size=len(clusters), replace=True)
            sample = [item for cluster in sampled_clusters for item in cluster_rows[str(cluster)]]
            estimates = _point_estimates(
                sample,
                clip_weight=clip_weight,
                switch_threshold=switch_threshold,
            )
            for name, value in estimates.items():
                if np.isfinite(value):
                    bootstrap[name].append(value)

    def result(name: str) -> OPEEstimate:
        values = bootstrap[name]
        interval = None
        if len(values) >= max(100, bootstrap_samples // 2):
            low, high = np.percentile(np.asarray(values), [2.5, 97.5])
            interval = (float(low), float(high))
        return OPEEstimate(value=points[name], confidence_interval_95=interval)

    importance_weights = np.asarray(
        [
            observation.target_probabilities[observation.selected_arm]
            / observation.behavior_probabilities[observation.selected_arm]
            for observation in observations
        ],
        dtype=np.float64,
    )
    effective_sample_size = float(np.sum(importance_weights) ** 2 / np.sum(importance_weights**2))
    ordered = sorted(observations, key=lambda item: item.decision_id)
    target_assignments_sha256 = policy_payload_sha256(
        [
            {
                "decision_id": item.decision_id,
                "target_probabilities": item.target_probabilities,
            }
            for item in ordered
        ]
    )
    evaluation_rows_sha256 = policy_payload_sha256(
        [
            {
                "decision_id": item.decision_id,
                "cluster_id": item.cluster_id,
                "selected_arm": item.selected_arm,
                "reward": item.reward,
                "behavior_probabilities": item.behavior_probabilities,
                "target_probabilities": item.target_probabilities,
                "direct_outcome_estimates": item.direct_outcome_estimates,
                "supports_ope": item.supports_ope,
            }
            for item in ordered
        ]
    )
    behavior_probability_values = np.asarray(
        [value for item in observations for value in item.behavior_probabilities.values()],
        dtype=np.float64,
    )
    target_probability_values = np.asarray(
        [value for item in observations for value in item.target_probabilities.values()],
        dtype=np.float64,
    )
    diagnostics: dict[str, float | int | bool] = {
        "overlap_satisfied": True,
        "effective_sample_size": effective_sample_size,
        "effective_sample_fraction": effective_sample_size / len(observations),
        "importance_weight_max": float(np.max(importance_weights)),
        "importance_weight_p95": float(np.percentile(importance_weights, 95)),
        "importance_weight_p99": float(np.percentile(importance_weights, 99)),
        "behavior_probability_min": float(np.min(behavior_probability_values)),
        "target_probability_min": float(np.min(target_probability_values)),
        "weights_above_clip": int(np.sum(importance_weights > clip_weight)),
        "weights_above_switch": int(np.sum(importance_weights > switch_threshold)),
        "clip_weight": clip_weight,
        "switch_threshold": switch_threshold,
        "bootstrap_samples_completed": min(
            (len(values) for values in bootstrap.values()),
            default=0,
        ),
        "estimator_range": float(max(points.values()) - min(points.values())),
    }
    return OPEResult(
        target_policy_name=target_policy.name,
        target_policy_version=target_policy.version,
        target_policy_sha256=target_policy.sha256,
        target_assignments_sha256=target_assignments_sha256,
        evaluation_rows_sha256=evaluation_rows_sha256,
        observations=len(observations),
        clusters=len(clusters),
        direct_method=result("direct_method"),
        ips=result("ips"),
        snips=result("snips"),
        doubly_robust=result("doubly_robust"),
        clipped_doubly_robust=result("clipped_doubly_robust"),
        switch_doubly_robust=result("switch_doubly_robust"),
        diagnostics=diagnostics,
    )


__all__ = [
    "OPEEstimate",
    "OPEObservation",
    "OPEResult",
    "OffPolicyEvaluationError",
    "TargetPolicyDefinition",
    "evaluate_target_policy",
]
