"""Versioned policy, outcome-window, and research-analysis contracts.

These models deliberately distinguish deterministic product decisions from
randomized research decisions.  A one-hot deterministic choice is not exposed
as a propensity and can never satisfy the ``supports_ope`` invariant.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cortex.libs.schemas.temporal import DualClockModel

PolicyMode = Literal["deterministic", "research_randomized", "legacy_diagnostic"]
PolicyArm = Literal[
    "no_action",
    "suggest_only",
    "workspace_simplify",
    "task_decompose",
    "breath_box",
    "nature_break",
    "flow_shield",
    "defusion_prompt",
    "circuit_breaker",
]
DeliveryStatus = Literal["delivered", "not_delivered", "not_applicable"]
OutcomeWindowStatus = Literal["pending", "finalized", "censored"]
PolicyObservationKind = Literal[
    "user_rating",
    "user_action",
    "undo",
    "restore_failure",
]


def canonical_policy_json(value: object) -> str:
    """Return the sole canonical representation used by policy digests."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def policy_payload_sha256(value: object) -> str:
    return hashlib.sha256(canonical_policy_json(value).encode("utf-8")).hexdigest()


class PolicyContextSnapshot(BaseModel):
    """Minimal, content-free context frozen at a policy decision point."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["policy-context/2.0"] = "policy-context/2.0"
    support_state: str = Field(..., min_length=1, max_length=32)
    support_status: str = Field(..., min_length=1, max_length=32)
    support_confidence: float = Field(..., ge=0.0, le=1.0)
    evidence_coverage: float = Field(..., ge=0.0, le=1.0)
    complexity_score: float = Field(..., ge=0.0, le=1.0)
    tab_count: int = Field(..., ge=0, le=10_000)
    error_count: int = Field(..., ge=0, le=100_000)
    thrashing_score: float = Field(..., ge=0.0, le=1.0)
    hour_utc: int = Field(..., ge=0, le=23)

    def normalized_features(self) -> tuple[float, ...]:
        """Versioned bounded vector with an explicit intercept."""

        state_codes = {
            "UNKNOWN": 0.0,
            "FLOW": 0.25,
            "RECOVERY": 0.5,
            "HYPO": 0.75,
            "HYPER": 1.0,
        }
        return (
            1.0,
            state_codes.get(self.support_state.upper(), 0.0),
            self.support_confidence,
            self.evidence_coverage,
            self.complexity_score,
            min(1.0, self.tab_count / 20.0),
            min(1.0, self.error_count / 10.0),
            self.thrashing_score,
            self.hour_utc / 23.0,
        )


class PolicyDecisionRecord(BaseModel):
    """Complete immutable decision-point record written before delivery."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["policy-decision/2.0"] = "policy-decision/2.0"
    decision_id: UUID = Field(default_factory=uuid4)
    decision_point_id: UUID
    session_id: str = Field(..., min_length=1, max_length=128)
    policy_name: str = Field(..., min_length=1, max_length=64)
    policy_version: str = Field(..., min_length=1, max_length=64)
    policy_mode: PolicyMode
    policy_state_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    context: PolicyContextSnapshot
    eligible: bool
    available: bool
    availability_reason: str = Field(..., min_length=1, max_length=160)
    feasible_arms: tuple[PolicyArm, ...]
    propensities: dict[PolicyArm, float] | None = None
    selected_arm: PolicyArm
    selected_probability: float = Field(..., ge=0.0, le=1.0)
    supports_ope: bool = False
    randomization_id: UUID | None = None
    random_seed_hex: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")
    random_counter: int | None = Field(None, ge=0)
    research_study_id: str | None = Field(None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
    research_study_epoch: str | None = Field(None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
    research_consent_version: str | None = Field(None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    research_specification_sha256: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")
    reward_version: str = Field(..., min_length=1, max_length=64)
    occurred_at_unix_ms: int = Field(..., ge=0)
    occurred_at_mono_ns: int = Field(..., ge=0)
    boot_id: UUID

    @field_validator("feasible_arms")
    @classmethod
    def _feasible_arms_are_canonical(cls, value: tuple[PolicyArm, ...]) -> tuple[PolicyArm, ...]:
        if not value or len(value) != len(set(value)):
            raise ValueError("feasible_arms must be non-empty and unique")
        if "no_action" not in value:
            raise ValueError("no_action must remain feasible at every decision point")
        return value

    @model_validator(mode="after")
    def _validate_selection_contract(self) -> PolicyDecisionRecord:
        if self.selected_arm not in self.feasible_arms:
            raise ValueError("selected_arm is not feasible")
        if self.policy_mode == "research_randomized":
            if not self.supports_ope:
                raise ValueError("randomized research decisions must declare OPE support")
            if self.propensities is None:
                raise ValueError("randomized research decisions require propensities")
            if set(self.propensities) != set(self.feasible_arms):
                raise ValueError("propensities must cover exactly the feasible arms")
            if any(
                not math.isfinite(value) or value <= 0.0 for value in self.propensities.values()
            ):
                raise ValueError("every feasible research arm needs positive probability")
            if not math.isclose(sum(self.propensities.values()), 1.0, abs_tol=1e-9):
                raise ValueError("propensities must sum to one")
            if not math.isclose(
                self.selected_probability,
                self.propensities[self.selected_arm],
                abs_tol=1e-12,
            ):
                raise ValueError("selected_probability does not match the logged propensity")
            if (
                self.randomization_id is None
                or self.random_seed_hex is None
                or self.random_counter is None
                or self.research_study_id is None
                or self.research_study_epoch is None
                or self.research_consent_version is None
                or self.research_specification_sha256 is None
            ):
                raise ValueError(
                    "randomized decisions require study/consent, seed, counter, and draw identity"
                )
        elif (
            self.propensities is not None
            or self.supports_ope
            or self.randomization_id is not None
            or self.random_seed_hex is not None
            or self.random_counter is not None
            or self.research_study_id is not None
            or self.research_study_epoch is not None
            or self.research_consent_version is not None
            or self.research_specification_sha256 is not None
        ):
            raise ValueError("deterministic/legacy records cannot masquerade as randomized data")
        elif self.selected_probability != 1.0:
            raise ValueError("deterministic selected_probability must be exactly one")
        if not self.eligible or not self.available:
            if self.selected_arm != "no_action":
                raise ValueError("ineligible or unavailable decision points must choose no_action")
        return self


class PolicyDeliveryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["policy-delivery/2.0"] = "policy-delivery/2.0"
    decision_id: UUID
    status: DeliveryStatus
    delivered_at_unix_ms: int | None = Field(None, ge=0)
    intervention_id: str | None = Field(None, max_length=128)
    reason: str = Field(..., min_length=1, max_length=160)

    @model_validator(mode="after")
    def _delivery_is_consistent(self) -> PolicyDeliveryRecord:
        if self.status == "delivered" and (
            self.delivered_at_unix_ms is None or not self.intervention_id
        ):
            raise ValueError("delivered records require time and intervention id")
        if self.status != "delivered" and self.delivered_at_unix_ms is not None:
            raise ValueError("non-deliveries cannot have a delivered timestamp")
        return self


class PolicyObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["policy-observation/2.0"] = "policy-observation/2.0"
    observation_id: UUID = Field(default_factory=uuid4)
    decision_id: UUID
    reward_version: str = Field(..., min_length=1, max_length=64)
    kind: PolicyObservationKind
    idempotency_key: str = Field(..., min_length=1, max_length=160)
    observed_at_unix_ms: int = Field(..., ge=0)
    payload: dict[str, Any]


class PolicyRewardRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["policy-reward/2.0"] = "policy-reward/2.0"
    decision_id: UUID
    reward_version: str = Field(..., min_length=1, max_length=64)
    value: float = Field(..., ge=-1.0, le=1.0)
    finalized_at_unix_ms: int = Field(..., ge=0)
    components: dict[str, float | str | bool | None]


class MRTStudySpecification(BaseModel):
    """Prespecified two-arm MRT analysis contract embedded in every export."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["mrt-spec/1.0"] = "mrt-spec/1.0"
    study_id: str = Field(..., pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
    study_epoch: str = Field(..., pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
    consent_version: str = Field(..., pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    policy_name: str = Field(..., min_length=1, max_length=64)
    policy_version: str = Field(..., min_length=1, max_length=64)
    action_catalog: tuple[
        Literal["no_action", "suggest_only"], Literal["no_action", "suggest_only"]
    ]
    reward_version: str = Field(..., min_length=1, max_length=64)
    proximal_window_seconds: int = Field(..., ge=30, le=86_400)
    estimand: Literal["marginal_proximal_effect_suggest_only_vs_no_action"] = (
        "marginal_proximal_effect_suggest_only_vs_no_action"
    )
    eligibility_rule: str = Field(..., min_length=1, max_length=500)
    availability_rule: str = Field(..., min_length=1, max_length=500)
    missing_outcome_rule: str = Field(..., min_length=1, max_length=500)
    contamination_rule: str = Field(..., min_length=1, max_length=500)
    cluster_key: Literal["session_id"] = "session_id"
    bootstrap_samples: int = Field(1_000, ge=200, le=100_000)
    analysis_seed: int = Field(..., ge=0, le=2**63 - 1)

    @field_validator("action_catalog")
    @classmethod
    def _catalog_has_control_first(
        cls,
        value: tuple[
            Literal["no_action", "suggest_only"],
            Literal["no_action", "suggest_only"],
        ],
    ) -> tuple[Literal["no_action", "suggest_only"], Literal["no_action", "suggest_only"]]:
        if value != ("no_action", "suggest_only"):
            raise ValueError("the first supported MRT epoch is fixed to no_action/suggest_only")
        return value


class PolicyDiagnosticsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day_utc: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("day_utc")
    @classmethod
    def _day_is_real_iso_date(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("day_utc must be a real ISO calendar date") from exc
        if parsed.isoformat() != value:
            raise ValueError("day_utc must use canonical YYYY-MM-DD form")
        return value


class PolicyDiagnosticsResponse(DualClockModel):
    filename: str = Field(..., pattern=r"^policy_diagnostics_\d{4}-\d{2}-\d{2}\.md$")
    sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    report_kind: Literal["descriptive_policy_diagnostics"] = "descriptive_policy_diagnostics"
    causal_claim: Literal[False] = False


class MRTExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    specification: MRTStudySpecification
    confirmation: Literal["EXPORT CONSENTED RESEARCH DATA"]


class MRTExportResponse(DualClockModel):
    filename: str = Field(..., pattern=r"^mrt_[A-Za-z0-9._-]+\.json$")
    sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    specification_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    row_count: int = Field(..., ge=0)


class MRTAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(..., pattern=r"^mrt_[A-Za-z0-9._-]+\.json$")


class MRTAnalysisResponse(DualClockModel):
    source_filename: str
    report_filename: str
    analysis: dict[str, Any]


__all__ = [
    "DeliveryStatus",
    "MRTAnalysisRequest",
    "MRTAnalysisResponse",
    "MRTExportRequest",
    "MRTExportResponse",
    "MRTStudySpecification",
    "OutcomeWindowStatus",
    "PolicyArm",
    "PolicyContextSnapshot",
    "PolicyDecisionRecord",
    "PolicyDeliveryRecord",
    "PolicyMode",
    "PolicyObservation",
    "PolicyObservationKind",
    "PolicyDiagnosticsRequest",
    "PolicyDiagnosticsResponse",
    "PolicyRewardRecord",
    "canonical_policy_json",
    "policy_payload_sha256",
]
