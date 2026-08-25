"""Separately consented, reproducible research randomization policy.

The first supported study epoch is a two-arm MRT (no action vs a low-risk
suggestion).  Online updates are disabled by default.  The optional linear
model exists for reviewed research configurations and uses an intercept,
bounded normalized features, regularization, and Cholesky solves—never an
explicit matrix inverse.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid5

import numpy as np
from numpy.typing import NDArray

from cortex.application.clock import Clock
from cortex.libs.schemas.policy import (
    PolicyArm,
    PolicyDecisionRecord,
    policy_payload_sha256,
)
from cortex.services.eval.production_policy import PolicySelectionInput

_RANDOMIZATION_NAMESPACE = UUID("671d7fa7-b024-4bbb-931a-b33b77ce95be")
RESEARCH_POLICY_NAME = "cortex-research-mrt"
RESEARCH_POLICY_VERSION = "2.0.0"


def _solve_spd(
    matrix: NDArray[np.float64],
    vector: NDArray[np.float64],
) -> NDArray[np.float64]:
    """Solve a regularized symmetric-positive-definite system stably."""

    chol = np.linalg.cholesky(matrix)
    return np.asarray(
        np.linalg.solve(chol.T, np.linalg.solve(chol, vector)),
        dtype=np.float64,
    )


@dataclass(frozen=True, slots=True)
class ResearchPolicySettings:
    study_id: str
    study_epoch: str
    consent_version: str
    seed_hex: str
    specification_sha256: str
    action_catalog: tuple[PolicyArm, ...] = ("no_action", "suggest_only")
    minimum_probability: float = 0.10
    temperature: float = 1.0
    regularization: float = 5.0
    online_learning: bool = False

    def __post_init__(self) -> None:
        identifier = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
        consent_identifier = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
        if (
            identifier.fullmatch(self.study_id) is None
            or identifier.fullmatch(self.study_epoch) is None
            or consent_identifier.fullmatch(self.consent_version) is None
        ):
            raise ValueError("research study, epoch, and separate consent version are required")
        if len(self.seed_hex) != 64:
            raise ValueError("seed_hex must contain exactly 32 bytes")
        try:
            bytes.fromhex(self.seed_hex)
        except ValueError as exc:
            raise ValueError("seed_hex must be lowercase hexadecimal") from exc
        if self.seed_hex.lower() != self.seed_hex:
            raise ValueError("seed_hex must be lowercase hexadecimal")
        if re.fullmatch(r"[0-9a-f]{64}", self.specification_sha256) is None:
            raise ValueError("specification_sha256 must be lowercase SHA-256")
        if self.action_catalog != ("no_action", "suggest_only"):
            raise ValueError("the reviewed first MRT epoch supports no_action/suggest_only only")
        if not 0.0 < self.minimum_probability <= 0.5:
            raise ValueError("minimum_probability must be in (0, 0.5]")
        if self.temperature <= 0.0 or self.regularization <= 0.0:
            raise ValueError("temperature and regularization must be positive")


class ResearchRandomizedPolicy:
    """Reproducible masked randomization with checksummed sufficient state."""

    policy_name = RESEARCH_POLICY_NAME
    policy_version = RESEARCH_POLICY_VERSION
    feature_count = 9

    def __init__(
        self,
        settings: ResearchPolicySettings,
        *,
        clock: Clock,
        state: dict[str, Any] | None = None,
    ) -> None:
        self.settings = settings
        self._clock = clock
        self._counter = 0
        arm_count = len(settings.action_catalog)
        self._a: NDArray[np.float64] = np.stack(
            [np.eye(self.feature_count, dtype=np.float64) * settings.regularization] * arm_count
        )
        self._b: NDArray[np.float64] = np.zeros((arm_count, self.feature_count), dtype=np.float64)
        self._counts: NDArray[np.int64] = np.zeros(arm_count, dtype=np.int64)
        if state is not None:
            self.restore_state(state)

    @property
    def counter(self) -> int:
        return self._counter

    def state(self) -> dict[str, Any]:
        state = {
            "schema_version": "research-policy-state/2.0",
            "policy_name": self.policy_name,
            "policy_version": self.policy_version,
            "study_id": self.settings.study_id,
            "study_epoch": self.settings.study_epoch,
            "consent_version": self.settings.consent_version,
            "seed_hex": self.settings.seed_hex,
            "specification_sha256": self.settings.specification_sha256,
            "action_catalog": list(self.settings.action_catalog),
            "minimum_probability": self.settings.minimum_probability,
            "temperature": self.settings.temperature,
            "regularization": self.settings.regularization,
            "online_learning": self.settings.online_learning,
            "counter": self._counter,
            "a_matrices": self._a.tolist(),
            "b_vectors": self._b.tolist(),
            "counts": self._counts.tolist(),
        }
        state["state_sha256"] = policy_payload_sha256(state)
        return state

    def restore_state(self, state: dict[str, Any]) -> None:
        supplied = dict(state)
        digest = str(supplied.pop("state_sha256", ""))
        if digest != policy_payload_sha256(supplied):
            raise ValueError("research policy state checksum mismatch")
        identity = (
            supplied.get("policy_name"),
            supplied.get("policy_version"),
            supplied.get("study_id"),
            supplied.get("study_epoch"),
            tuple(supplied.get("action_catalog", [])),
        )
        expected = (
            self.policy_name,
            self.policy_version,
            self.settings.study_id,
            self.settings.study_epoch,
            self.settings.action_catalog,
        )
        if identity != expected:
            raise ValueError("research policy state belongs to another fixed study epoch")
        fixed_settings = (
            supplied.get("consent_version"),
            supplied.get("seed_hex"),
            supplied.get("specification_sha256"),
            supplied.get("minimum_probability"),
            supplied.get("temperature"),
            supplied.get("regularization"),
            supplied.get("online_learning"),
        )
        expected_settings = (
            self.settings.consent_version,
            self.settings.seed_hex,
            self.settings.specification_sha256,
            self.settings.minimum_probability,
            self.settings.temperature,
            self.settings.regularization,
            self.settings.online_learning,
        )
        if fixed_settings != expected_settings:
            raise ValueError("research policy state differs from the frozen epoch settings")
        a = np.asarray(supplied.get("a_matrices"), dtype=np.float64)
        b = np.asarray(supplied.get("b_vectors"), dtype=np.float64)
        counts = np.asarray(supplied.get("counts"), dtype=np.int64)
        expected_a = (len(self.settings.action_catalog), self.feature_count, self.feature_count)
        if a.shape != expected_a or b.shape != expected_a[:2] or counts.shape != (expected_a[0],):
            raise ValueError("research policy state dimensions are incompatible")
        if not np.all(np.isfinite(a)) or not np.all(np.isfinite(b)) or np.any(counts < 0):
            raise ValueError("research policy state contains invalid numeric values")
        for matrix in a:
            if not np.allclose(matrix, matrix.T, atol=1e-10):
                raise ValueError("research policy precision matrix is not symmetric")
            np.linalg.cholesky(matrix)
        self._a = a
        self._b = b
        self._counts = counts
        self._counter = int(supplied.get("counter", -1))
        if self._counter < 0:
            raise ValueError("research policy counter is invalid")

    @property
    def state_sha256(self) -> str:
        return str(self.state()["state_sha256"])

    def _probabilities(
        self,
        features: NDArray[np.float64],
        feasible: tuple[PolicyArm, ...],
    ) -> dict[PolicyArm, float]:
        catalog = self.settings.action_catalog
        feasible_catalog = [arm for arm in catalog if arm in feasible]
        if not feasible_catalog:
            return {"no_action": 1.0}
        if len(feasible_catalog) == 1:
            return {feasible_catalog[0]: 1.0}

        if self.settings.online_learning:
            scores: list[float] = []
            for index, arm in enumerate(catalog):
                if arm not in feasible_catalog:
                    scores.append(float("-inf"))
                    continue
                theta = _solve_spd(self._a[index], self._b[index])
                scores.append(float(theta @ features) / self.settings.temperature)
            finite = np.asarray([score for score in scores if math.isfinite(score)])
            offset = float(np.max(finite))
            raw = {
                arm: math.exp(scores[index] - offset)
                for index, arm in enumerate(catalog)
                if arm in feasible_catalog
            }
        else:
            raw = dict.fromkeys(feasible_catalog, 1.0)

        total = float(sum(raw.values()))
        base = {arm: float(value / total) for arm, value in raw.items()}
        floor = self.settings.minimum_probability
        mixed = {
            arm: (1.0 - floor * len(base)) * probability + floor
            for arm, probability in base.items()
        }
        normalization = sum(mixed.values())
        return {arm: probability / normalization for arm, probability in mixed.items()}

    def choose(self, request: PolicySelectionInput) -> tuple[PolicyDecisionRecord, dict[str, Any]]:
        feasible = tuple(
            arm for arm in self.settings.action_catalog if arm in request.feasible_arms
        )
        if not request.eligible or not request.available:
            # An unavailable point is logged but not randomized and therefore
            # excluded from analysis. It intentionally does not claim OPE.
            raise ValueError("research randomization may only run at eligible, available points")
        if feasible != self.settings.action_catalog:
            raise ValueError(
                "research randomization requires positive support for the frozen action catalog"
            )

        features = np.asarray(request.context.normalized_features(), dtype=np.float64)
        if features.shape != (self.feature_count,) or not np.all(np.isfinite(features)):
            raise ValueError("invalid normalized research context")
        probabilities = self._probabilities(features, feasible)
        if set(probabilities) != set(feasible):
            raise ValueError("feasibility mask and behavior distribution diverged")

        counter = self._counter
        message = (
            f"{self.settings.study_id}|{self.settings.study_epoch}|"
            f"{request.decision_point_id}|{counter}"
        ).encode()
        digest = hmac.new(bytes.fromhex(self.settings.seed_hex), message, hashlib.sha256).digest()
        draw = int.from_bytes(digest[:8], "big") / float(2**64)
        cumulative = 0.0
        selected: PolicyArm = feasible[-1]
        for arm in feasible:
            cumulative += probabilities[arm]
            if draw < cumulative:
                selected = arm
                break
        randomization_id = uuid5(_RANDOMIZATION_NAMESPACE, digest.hex())
        self._counter += 1
        persisted_state = self.state()
        state_digest = str(persisted_state["state_sha256"])

        decision = PolicyDecisionRecord(
            decision_point_id=request.decision_point_id,
            session_id=request.session_id,
            policy_name=self.policy_name,
            policy_version=self.policy_version,
            policy_mode="research_randomized",
            policy_state_sha256=state_digest,
            context=request.context,
            eligible=True,
            available=True,
            availability_reason=request.availability_reason,
            feasible_arms=feasible,
            propensities=probabilities,
            selected_arm=selected,
            selected_probability=probabilities[selected],
            supports_ope=True,
            randomization_id=randomization_id,
            random_seed_hex=self.settings.seed_hex,
            random_counter=counter,
            research_study_id=self.settings.study_id,
            research_study_epoch=self.settings.study_epoch,
            research_consent_version=self.settings.consent_version,
            research_specification_sha256=self.settings.specification_sha256,
            reward_version=request.reward_version,
            occurred_at_unix_ms=self._clock.unix_ms(),
            occurred_at_mono_ns=self._clock.monotonic_ns(),
            boot_id=self._clock.boot_id,
        )
        return decision, persisted_state

    def update(self, context: tuple[float, ...], arm: PolicyArm, reward: float) -> None:
        """Apply one bounded sufficient-statistic update in research mode."""

        if not self.settings.online_learning:
            raise RuntimeError("online research learning is disabled for this study epoch")
        if arm not in self.settings.action_catalog:
            raise ValueError("arm is outside the frozen action catalog")
        x = np.asarray(context, dtype=np.float64)
        if x.shape != (self.feature_count,) or not np.all(np.isfinite(x)):
            raise ValueError("invalid normalized context")
        index = self.settings.action_catalog.index(arm)
        bounded_reward = float(np.clip(reward, -1.0, 1.0))
        candidate = self._a[index] + np.outer(x, x)
        if float(np.linalg.norm(candidate, ord=2)) > 1_000_000.0:
            raise ValueError("research sufficient statistics exceeded the fixed bound")
        np.linalg.cholesky(candidate)
        self._a[index] = candidate
        self._b[index] += bounded_reward * x
        self._counts[index] += 1


__all__ = [
    "RESEARCH_POLICY_NAME",
    "RESEARCH_POLICY_VERSION",
    "ResearchPolicySettings",
    "ResearchRandomizedPolicy",
]
