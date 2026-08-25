"""Operational boundary around production support inference and rollback."""

from __future__ import annotations

from cortex.libs.schemas.features import FeatureVector
from cortex.libs.schemas.state import (
    EstimateStatus,
    RuleEvaluation,
    SupportScores,
    SupportState,
)
from cortex.services.state_engine.model_registry import SupportModelRegistry
from cortex.services.state_engine.rule_scorer import RuleScorer


class SupportInferenceEngine:
    """Evaluate the active registered implementation.

    The safety-null entry is an operational rollback, not another heuristic:
    it always emits insufficient evidence and cannot trigger an intervention.
    """

    def __init__(
        self,
        scorer: RuleScorer,
        registry: SupportModelRegistry | None = None,
    ) -> None:
        self._scorer = scorer
        self.registry = registry or SupportModelRegistry()

    def replace_scorer(self, scorer: RuleScorer) -> None:
        """Atomically point future evaluations at a recalibrated scorer."""

        self._scorer = scorer

    def evaluate(self, vector: FeatureVector) -> RuleEvaluation:
        active = self.registry.active
        if active.kind == "safety_null":
            return RuleEvaluation(
                status=EstimateStatus.INSUFFICIENT_EVIDENCE,
                scores=SupportScores(),
                evidence_coverage=0.0,
                state_coverage=dict.fromkeys(SupportState, 0.0),
                exclusions=[
                    "Support inference is disabled by the safety-null rollback."
                ],
                model=active.identity,
            )
        if active.kind != "deterministic":
            raise RuntimeError(
                f"unsupported production inference kind: {active.kind}"
            )
        evaluation = self._scorer.evaluate(vector)
        if evaluation.model != active.identity:
            raise RuntimeError("scorer identity does not match active registry entry")
        return evaluation

