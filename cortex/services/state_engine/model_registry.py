"""Local registry and rollback boundary for support inference models.

No probabilistic model is shipped. The active entry is an explainable,
deterministic rule set; the only rollback target is a fail-closed no-inference
entry. A future probabilistic entry cannot be registered without naming its
separate probability-calibration artifact.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from cortex.libs.schemas.state import InferenceModelIdentity
from cortex.services.state_engine.feature_schema import (
    FEATURE_SCHEMA_VERSION,
    feature_schema_sha256,
)
from cortex.services.state_engine.generated_model_identity import (
    DETERMINISTIC_SUPPORT_IMPLEMENTATION_SHA256,
)

DETERMINISTIC_SUPPORT_VERSION = "2.1.1"
NO_INFERENCE_VERSION = "safety-null-v1"


@lru_cache(maxsize=1)
def deterministic_support_identity() -> InferenceModelIdentity:
    """Identity of the production Level-A rules."""

    return InferenceModelIdentity(
        name="deterministic-support",
        version=DETERMINISTIC_SUPPORT_VERSION,
        feature_schema_version=FEATURE_SCHEMA_VERSION,
        implementation_sha256=DETERMINISTIC_SUPPORT_IMPLEMENTATION_SHA256,
        validation_status="deterministic_rules",
    )


@lru_cache(maxsize=1)
def no_inference_identity() -> InferenceModelIdentity:
    """Fail-closed rollback target that can only emit UNKNOWN."""

    return InferenceModelIdentity(
        name="no-inference",
        version=NO_INFERENCE_VERSION,
        feature_schema_version=FEATURE_SCHEMA_VERSION,
        implementation_sha256=feature_schema_sha256(),
        validation_status="deterministic_rules",
    )


@dataclass(frozen=True, slots=True)
class RegisteredModel:
    identity: InferenceModelIdentity
    kind: Literal["deterministic", "probabilistic", "safety_null"]
    model_card: str
    production_eligible: bool

    def __post_init__(self) -> None:
        if (
            self.kind == "probabilistic"
            and self.identity.probability_calibration_artifact_id is None
        ):
            raise ValueError(
                "probabilistic models require a probability calibration artifact"
            )


class SupportModelRegistry:
    """Small explicit registry with a safe rollback operation."""

    def __init__(self) -> None:
        self._entries = {
            DETERMINISTIC_SUPPORT_VERSION: RegisteredModel(
                identity=deterministic_support_identity(),
                kind="deterministic",
                model_card="docs/model-cards/deterministic-support-v2.md",
                production_eligible=True,
            ),
            NO_INFERENCE_VERSION: RegisteredModel(
                identity=no_inference_identity(),
                kind="safety_null",
                model_card="docs/model-cards/no-inference-safety-fallback.md",
                production_eligible=True,
            ),
        }
        self._active_version = DETERMINISTIC_SUPPORT_VERSION

    @property
    def active(self) -> RegisteredModel:
        return self._entries[self._active_version]

    def activate(self, version: str) -> RegisteredModel:
        entry = self._entries.get(version)
        if entry is None or not entry.production_eligible:
            raise ValueError(f"model version is not production eligible: {version}")
        self._active_version = version
        return entry

    def rollback_to_safe_null(self) -> RegisteredModel:
        """Disable inference without restoring an unsafe legacy classifier."""

        return self.activate(NO_INFERENCE_VERSION)

    def registered(self) -> tuple[RegisteredModel, ...]:
        return tuple(self._entries.values())

    def register(self, entry: RegisteredModel) -> None:
        """Register an explicit future entry without silently replacing one."""

        version = entry.identity.version
        if version in self._entries:
            raise ValueError(f"model version is already registered: {version}")
        self._entries[version] = entry
