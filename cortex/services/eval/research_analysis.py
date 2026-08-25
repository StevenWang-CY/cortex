"""Immutable MRT export and prespecified weighted/centered analysis.

This module is intentionally independent of runtime policy selection.  It
accepts only ``research_randomized`` records from one frozen study epoch and
recomputes every estimate from the exported artifact.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
from numpy.typing import NDArray

from cortex.application.clock import Clock
from cortex.libs.schemas.policy import (
    MRTStudySpecification,
    canonical_policy_json,
    policy_payload_sha256,
)
from cortex.libs.utils.atomic_write import atomic_write_text
from cortex.services.eval.policy_repository import PolicyRepository


class ResearchExportError(RuntimeError):
    """Research data fail a frozen specification or integrity invariant."""


def _write_new_private_file(path: Path, text: str) -> None:
    """Durably create ``path`` without ever replacing existing evidence."""

    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = None
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if hasattr(os, "O_DIRECTORY"):
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            try:
                path.unlink()
            except OSError:
                pass
        raise


@dataclass(frozen=True, slots=True)
class MRTAnalysisResult:
    study_id: str
    study_epoch: str
    estimand: str
    included_decision_points: int
    excluded_decision_points: int
    cluster_count: int
    effect: float
    standard_error_cluster_robust: float | None
    confidence_interval_95: tuple[float, float] | None
    bootstrap_samples: int
    treatment_probability_min: float
    treatment_probability_max: float
    effective_sample_size: float
    export_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "mrt-analysis/1.0",
            "study_id": self.study_id,
            "study_epoch": self.study_epoch,
            "estimand": self.estimand,
            "included_decision_points": self.included_decision_points,
            "excluded_decision_points": self.excluded_decision_points,
            "cluster_count": self.cluster_count,
            "effect": self.effect,
            "standard_error_cluster_robust": self.standard_error_cluster_robust,
            "confidence_interval_95": list(self.confidence_interval_95)
            if self.confidence_interval_95 is not None
            else None,
            "bootstrap_samples": self.bootstrap_samples,
            "treatment_probability_min": self.treatment_probability_min,
            "treatment_probability_max": self.treatment_probability_max,
            "effective_sample_size": self.effective_sample_size,
            "export_sha256": self.export_sha256,
            "interpretation": (
                "Prespecified marginal proximal effect estimate for this consented MRT "
                "epoch. Validity depends on randomization integrity, the stated missingness "
                "and contamination rules, and the study's independent method review."
            ),
        }


async def export_mrt_dataset(
    repository: PolicyRepository,
    specification: MRTStudySpecification,
    destination_directory: str | Path,
    *,
    clock: Clock,
) -> Path:
    """Create a new canonical, checksummed, non-overwriting research export."""

    specification_payload = specification.model_dump(mode="json")
    specification_sha = policy_payload_sha256(specification_payload)
    source_rows = await repository.export_rows(policy_mode="research_randomized")
    rows: list[dict[str, Any]] = []
    seen_randomization_ids: set[str] = set()
    seen_draws: set[tuple[str, int]] = set()
    for record in source_rows:
        decision = record["decision"]
        if (
            decision.get("research_study_id") != specification.study_id
            or decision.get("research_study_epoch") != specification.study_epoch
        ):
            continue
        if decision.get("research_consent_version") != specification.consent_version:
            raise ResearchExportError("decision consent version differs from the study spec")
        if decision.get("research_specification_sha256") != specification_sha:
            raise ResearchExportError("decision is not bound to the exported study spec")
        if (
            decision.get("policy_name") != specification.policy_name
            or decision.get("policy_version") != specification.policy_version
        ):
            raise ResearchExportError("decision policy identity differs from the study spec")
        if decision.get("reward_version") != specification.reward_version:
            raise ResearchExportError("decision reward version differs from the study spec")
        propensities = decision.get("propensities")
        feasible = tuple(decision.get("feasible_arms") or ())
        if decision.get("eligible") is not True or decision.get("available") is not True:
            raise ResearchExportError("research log contains an unavailable randomization")
        if decision.get("supports_ope") is not True or not isinstance(propensities, dict):
            raise ResearchExportError("randomized decision lacks a complete propensity record")
        if feasible != specification.action_catalog or tuple(propensities) != feasible:
            raise ResearchExportError(
                "randomized decision differs from the frozen ordered action catalog"
            )
        propensity_values = np.asarray(list(propensities.values()), dtype=np.float64)
        if (
            not np.all(np.isfinite(propensity_values))
            or np.any(propensity_values <= 0.0)
            or np.any(propensity_values >= 1.0)
            or not np.isclose(float(np.sum(propensity_values)), 1.0)
        ):
            raise ResearchExportError("behavior propensities do not sum to one")
        selected = str(decision.get("selected_arm"))
        if selected not in specification.action_catalog:
            raise ResearchExportError("selected arm is outside the frozen study catalog")
        if not np.isclose(
            float(decision.get("selected_probability", -1.0)),
            float(propensities[selected]),
        ):
            raise ResearchExportError("selected-arm probability differs from logged propensity")
        randomization_id = str(decision.get("randomization_id") or "")
        random_seed = str(decision.get("random_seed_hex") or "")
        try:
            random_counter = int(decision["random_counter"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ResearchExportError("research draw identity is incomplete") from exc
        draw_identity = (random_seed, random_counter)
        if (
            not randomization_id
            or len(random_seed) != 64
            or random_counter < 0
            or randomization_id in seen_randomization_ids
            or draw_identity in seen_draws
        ):
            raise ResearchExportError("research draw identity is missing or duplicated")
        seen_randomization_ids.add(randomization_id)
        seen_draws.add(draw_identity)
        outcome = record.get("outcome")
        reward = record.get("reward")
        contamination = (
            list(outcome.get("contamination") or []) if isinstance(outcome, dict) else []
        )
        complete = (
            isinstance(outcome, dict)
            and outcome.get("status") == "finalized"
            and isinstance(reward, dict)
            and reward.get("version") == specification.reward_version
        )
        if isinstance(outcome, dict):
            opened_at = int(outcome.get("window_opened_at_unix_ms", -1))
            closes_at = int(outcome.get("scheduled_close_at_unix_ms", -1))
            if closes_at - opened_at != specification.proximal_window_seconds * 1_000:
                raise ResearchExportError("outcome window differs from the frozen MRT spec")
        included = complete and not contamination
        rows.append(
            {
                "decision_id": decision["decision_id"],
                "decision_point_id": decision["decision_point_id"],
                "session_id": decision["session_id"],
                "occurred_at_unix_ms": decision["occurred_at_unix_ms"],
                "eligible": decision["eligible"],
                "available": decision["available"],
                "availability_reason": decision["availability_reason"],
                "feasible_arms": list(feasible),
                "behavior_propensities": propensities,
                "selected_arm": selected,
                "selected_probability": decision["selected_probability"],
                "randomization_id": decision["randomization_id"],
                "random_seed_hex": decision["random_seed_hex"],
                "random_counter": decision["random_counter"],
                "context": decision["context"],
                "delivery": record.get("delivery"),
                "outcome_status": outcome.get("status") if isinstance(outcome, dict) else None,
                "contamination": contamination,
                "reward": float(reward["value"]) if isinstance(reward, dict) else None,
                "reward_components": reward.get("components") if isinstance(reward, dict) else None,
                "analysis_included": included,
                "exclusion_reason": (
                    None
                    if included
                    else (
                        "contaminated_window"
                        if contamination
                        else "missing_or_censored_proximal_outcome"
                    )
                ),
            }
        )

    export_id = uuid4()
    document = {
        "schema_version": "mrt-export/1.0",
        "export_id": str(export_id),
        "generated_at_unix_ms": clock.unix_ms(),
        "specification": specification_payload,
        "specification_sha256": specification_sha,
        "source": "cortex-sqlite-policy-lifecycle-v2",
        "row_count": len(rows),
        "included_row_count": sum(1 for row in rows if row["analysis_included"]),
        "rows": rows,
        "notice": (
            "Immutable local research export. It contains derived context and feedback, "
            "not raw frames, waveform samples, code, terminal text, or full URLs."
        ),
    }
    encoded = canonical_policy_json(document)
    dataset_sha = hashlib.sha256(encoded.encode()).hexdigest()
    destination_root = Path(destination_directory).expanduser().resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    try:
        destination_root.chmod(0o700)
    except OSError as exc:
        raise ResearchExportError("research export directory is not private") from exc
    if destination_root.stat().st_mode & 0o077:
        raise ResearchExportError("research export directory remains accessible to other users")
    filename = f"mrt_{specification.study_id}_{specification.study_epoch}_{export_id}.json"
    destination = destination_root / filename
    sidecar = destination.with_suffix(destination.suffix + ".sha256")
    destination_created = False
    try:
        _write_new_private_file(destination, encoded)
        destination_created = True
        _write_new_private_file(sidecar, f"{dataset_sha}  {destination.name}\n")
    except FileExistsError as exc:
        if destination_created:
            try:
                destination.unlink()
            except OSError:
                pass
        raise ResearchExportError("research export filename already exists") from exc
    except BaseException:
        # This invocation created ``destination``; without a sidecar it is not
        # valid evidence and should not survive a reported export failure.
        if destination_created and not sidecar.exists():
            try:
                destination.unlink()
            except OSError:
                pass
        raise
    await repository.record_research_export(
        export_id=export_id,
        study_id=specification.study_id,
        study_epoch=specification.study_epoch,
        specification_sha256=specification_sha,
        dataset_sha256=dataset_sha,
        filename=filename,
        row_count=len(rows),
    )
    return destination


def _load_verified_export(path: str | Path) -> tuple[dict[str, Any], str]:
    source = Path(path).expanduser().resolve()
    payload = source.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    sidecar = source.with_suffix(source.suffix + ".sha256")
    if not sidecar.exists():
        raise ResearchExportError("immutable export checksum sidecar is missing")
    pieces = sidecar.read_text(encoding="utf-8").strip().split()
    if len(pieces) != 2 or pieces[0] != digest or pieces[1] != source.name:
        raise ResearchExportError("immutable export checksum verification failed")
    decoded = json.loads(payload)
    if not isinstance(decoded, dict) or decoded.get("schema_version") != "mrt-export/1.0":
        raise ResearchExportError("unsupported MRT export schema")
    specification = decoded.get("specification")
    if not isinstance(specification, dict):
        raise ResearchExportError("MRT export is missing its prespecification")
    if policy_payload_sha256(specification) != decoded.get("specification_sha256"):
        raise ResearchExportError("MRT prespecification checksum mismatch")
    return decoded, digest


def _wcls_fit(
    treatment: NDArray[np.float64],
    probability: NDArray[np.float64],
    outcome: NDArray[np.float64],
    clusters: NDArray[np.generic],
) -> tuple[float, float | None]:
    centered = treatment - probability
    design = np.column_stack((np.ones_like(centered), centered))
    weights = 1.0 / np.clip(probability * (1.0 - probability), 1e-8, None)
    gram = design.T @ (weights[:, None] * design)
    rhs = design.T @ (weights * outcome)
    try:
        chol = np.linalg.cholesky(gram)
        beta = np.linalg.solve(chol.T, np.linalg.solve(chol, rhs))
        bread = np.linalg.solve(chol.T, np.linalg.solve(chol, np.eye(2)))
    except np.linalg.LinAlgError as exc:
        raise ResearchExportError("WCLS design is singular") from exc
    residual = outcome - design @ beta
    unique_clusters = np.unique(clusters)
    if unique_clusters.size < 2:
        return float(beta[1]), None
    meat: NDArray[np.float64] = np.zeros((2, 2), dtype=np.float64)
    for cluster in unique_clusters:
        mask = clusters == cluster
        score = design[mask].T @ (weights[mask] * residual[mask])
        meat += np.outer(score, score)
    covariance = bread @ meat @ bread
    covariance *= unique_clusters.size / (unique_clusters.size - 1)
    standard_error = float(np.sqrt(max(0.0, covariance[1, 1])))
    return float(beta[1]), standard_error


def analyze_mrt_export(path: str | Path) -> MRTAnalysisResult:
    """Recompute the prespecified WCLS estimate and cluster bootstrap CI."""

    document, digest = _load_verified_export(path)
    specification = MRTStudySpecification.model_validate(document["specification"])
    raw_rows = document.get("rows")
    if not isinstance(raw_rows, list):
        raise ResearchExportError("MRT export rows are invalid")
    rows = [row for row in raw_rows if isinstance(row, dict) and row.get("analysis_included")]
    if not rows:
        raise ResearchExportError("MRT export has no complete, uncontaminated outcomes")
    treatment = np.asarray(
        [1.0 if row.get("selected_arm") == "suggest_only" else 0.0 for row in rows],
        dtype=np.float64,
    )
    probability = np.asarray(
        [float(row["behavior_propensities"]["suggest_only"]) for row in rows],
        dtype=np.float64,
    )
    outcome = np.asarray([float(row["reward"]) for row in rows], dtype=np.float64)
    clusters = np.asarray([str(row["session_id"]) for row in rows], dtype=object)
    if np.any(probability <= 0.0) or np.any(probability >= 1.0):
        raise ResearchExportError("MRT treatment probabilities violate overlap")
    if not np.all(np.isfinite(outcome)) or np.any(np.abs(outcome) > 1.0):
        raise ResearchExportError("MRT outcome values are invalid")
    effect, robust_se = _wcls_fit(treatment, probability, outcome, clusters)

    rng = np.random.default_rng(specification.analysis_seed)
    unique_clusters = np.unique(clusters)
    bootstrap_estimates: list[float] = []
    if unique_clusters.size >= 2:
        cluster_indices = {
            cluster: np.flatnonzero(clusters == cluster) for cluster in unique_clusters
        }
        for _ in range(specification.bootstrap_samples):
            sampled = rng.choice(unique_clusters, size=unique_clusters.size, replace=True)
            indices = np.concatenate([cluster_indices[cluster] for cluster in sampled])
            try:
                estimate, _ = _wcls_fit(
                    treatment[indices],
                    probability[indices],
                    outcome[indices],
                    np.arange(indices.size, dtype=np.int64),
                )
            except ResearchExportError:
                continue
            if np.isfinite(estimate):
                bootstrap_estimates.append(estimate)
    confidence_interval = None
    if len(bootstrap_estimates) >= max(100, specification.bootstrap_samples // 2):
        low, high = np.percentile(np.asarray(bootstrap_estimates), [2.5, 97.5])
        confidence_interval = (float(low), float(high))
    inverse_variance_weights = 1.0 / (probability * (1.0 - probability))
    effective_sample_size = float(
        np.sum(inverse_variance_weights) ** 2 / np.sum(inverse_variance_weights**2)
    )
    return MRTAnalysisResult(
        study_id=specification.study_id,
        study_epoch=specification.study_epoch,
        estimand=specification.estimand,
        included_decision_points=len(rows),
        excluded_decision_points=len(raw_rows) - len(rows),
        cluster_count=int(unique_clusters.size),
        effect=effect,
        standard_error_cluster_robust=robust_se,
        confidence_interval_95=confidence_interval,
        bootstrap_samples=len(bootstrap_estimates),
        treatment_probability_min=float(np.min(probability)),
        treatment_probability_max=float(np.max(probability)),
        effective_sample_size=effective_sample_size,
        export_sha256=digest,
    )


def write_mrt_analysis_report(
    export_path: str | Path,
    destination: str | Path,
) -> Path:
    result = analyze_mrt_export(export_path)
    payload = result.to_dict()
    target = Path(destination).expanduser().resolve()
    lines = [
        f"# MRT proximal-effect analysis — {result.study_id} / {result.study_epoch}",
        "",
        f"- Estimand: `{result.estimand}`",
        f"- Included decision points: {result.included_decision_points}",
        f"- Excluded decision points: {result.excluded_decision_points}",
        f"- Session clusters: {result.cluster_count}",
        f"- WCLS effect: {result.effect:.6f}",
        (
            f"- Cluster-robust SE: {result.standard_error_cluster_robust:.6f}"
            if result.standard_error_cluster_robust is not None
            else "- Cluster-robust SE: n/a (fewer than two clusters)"
        ),
        (
            "- Cluster-bootstrap 95% interval: "
            f"[{result.confidence_interval_95[0]:.6f}, {result.confidence_interval_95[1]:.6f}]"
            if result.confidence_interval_95 is not None
            else "- Cluster-bootstrap 95% interval: n/a"
        ),
        f"- Effective sample size diagnostic: {result.effective_sample_size:.2f}",
        f"- Source export SHA-256: `{result.export_sha256}`",
        "",
        "## Interpretation boundary",
        "",
        str(payload["interpretation"]),
        "Agreement with another estimator would be a sensitivity diagnostic, not proof. "
        "Do not generalize beyond the enrolled population, frozen epoch, proximal window, "
        "or intervention catalog without a new study and review.",
    ]
    atomic_write_text(target, "\n".join(lines) + "\n")
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return target


__all__ = [
    "MRTAnalysisResult",
    "ResearchExportError",
    "analyze_mrt_export",
    "export_mrt_dataset",
    "write_mrt_analysis_report",
]
