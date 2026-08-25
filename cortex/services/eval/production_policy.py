"""Deterministic, versioned production intervention policy.

Production selection is intentionally boring.  Safety, consent, capability,
and target-scope checks have already produced ``feasible_arms``; this policy
cannot add an arm or increase authority.  It does not learn online and it does
not emit behavior propensities suitable for causal/off-policy analysis.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

from cortex.application.clock import Clock
from cortex.libs.schemas.policy import (
    PolicyArm,
    PolicyContextSnapshot,
    PolicyDecisionRecord,
    canonical_policy_json,
    policy_payload_sha256,
)

PRODUCTION_POLICY_NAME: Final = "cortex-production-ordered-rules"
PRODUCTION_POLICY_VERSION: Final = "2.0.0"


@dataclass(frozen=True, slots=True)
class PolicySelectionInput:
    decision_point_id: UUID
    session_id: str
    context: PolicyContextSnapshot
    eligible: bool
    available: bool
    availability_reason: str
    feasible_arms: tuple[PolicyArm, ...]
    recent_repeated_dismissal: bool = False
    preferred_low_friction_arm: PolicyArm | None = None
    reward_version: str = "helpfulness-v2"


class DeterministicProductionPolicy:
    """Ordered rules from IMPLEMENTATION.md §11.4."""

    def __init__(self, *, clock: Clock) -> None:
        self._clock = clock
        self._state = {
            "algorithm": "ordered-rules",
            "policy_name": PRODUCTION_POLICY_NAME,
            "policy_version": PRODUCTION_POLICY_VERSION,
            "rules": [
                "unavailable_or_ineligible=>no_action",
                "repeated_dismissal=>no_action",
                "preferred_low_friction_feasible=>preferred",
                "suggest_only_feasible=>suggest_only",
                "fallback=>no_action",
            ],
            "online_learning": False,
            "supports_ope": False,
        }

    @property
    def state_json(self) -> str:
        return canonical_policy_json(self._state)

    def state(self) -> dict[str, object]:
        return dict(self._state)

    @property
    def state_sha256(self) -> str:
        return policy_payload_sha256(self._state)

    def choose(self, request: PolicySelectionInput) -> PolicyDecisionRecord:
        feasible = tuple(dict.fromkeys(request.feasible_arms))
        if "no_action" not in feasible:
            feasible = ("no_action", *feasible)

        if not request.eligible or not request.available:
            selected: PolicyArm = "no_action"
        elif request.recent_repeated_dismissal:
            selected = "no_action"
        elif (
            request.preferred_low_friction_arm is not None
            and request.preferred_low_friction_arm != "no_action"
            and request.preferred_low_friction_arm in feasible
        ):
            selected = request.preferred_low_friction_arm
        elif "suggest_only" in feasible:
            selected = "suggest_only"
        else:
            selected = "no_action"

        return PolicyDecisionRecord(
            decision_point_id=request.decision_point_id,
            session_id=request.session_id,
            policy_name=PRODUCTION_POLICY_NAME,
            policy_version=PRODUCTION_POLICY_VERSION,
            policy_mode="deterministic",
            policy_state_sha256=self.state_sha256,
            context=request.context,
            eligible=request.eligible,
            available=request.available,
            availability_reason=request.availability_reason,
            feasible_arms=feasible,
            propensities=None,
            selected_arm=selected,
            selected_probability=1.0,
            supports_ope=False,
            randomization_id=None,
            random_seed_hex=None,
            random_counter=None,
            research_study_id=None,
            research_study_epoch=None,
            research_consent_version=None,
            research_specification_sha256=None,
            reward_version=request.reward_version,
            occurred_at_unix_ms=self._clock.unix_ms(),
            occurred_at_mono_ns=self._clock.monotonic_ns(),
            boot_id=self._clock.boot_id,
        )


__all__ = [
    "DeterministicProductionPolicy",
    "PRODUCTION_POLICY_NAME",
    "PRODUCTION_POLICY_VERSION",
    "PolicySelectionInput",
]
