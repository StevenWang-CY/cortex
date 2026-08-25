"""
Cortex Evaluation Schemas

Models for local intervention-helpfulness diagnostics.

The bandit-shaped fields remain decode-only compatibility for legacy records;
the production policy and v2 research lifecycle use ``schemas.policy``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from cortex.application.clock import SYSTEM_CLOCK, utc_datetime


class InterventionSnapshot(BaseModel):
    """Workspace state snapshot for pre/post comparison."""
    state: str = Field(..., description="User state (FLOW/HYPER/HYPO/RECOVERY)")
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    complexity_score: float = Field(0.0, ge=0.0, le=1.0)
    tab_count: int = Field(0, ge=0)
    error_count: int = Field(0, ge=0)
    thrashing_score: float = Field(0.0, ge=0.0, le=1.0)
    stress_integral: float = Field(0.0, ge=0.0)
    timestamp: float = Field(
        0.0,
        deprecated=True,
        description="Deprecated v1 process-local monotonic seconds.",
    )
    schema_version: Literal["2.0"] = "2.0"
    observed_at_unix_ms: int | None = Field(None, ge=0)
    observed_at_mono_ns: int | None = Field(None, ge=0)
    boot_id: UUID | None = None

    @model_validator(mode="after")
    def _validate_v2_time_tuple(self) -> InterventionSnapshot:
        supplied = (
            self.observed_at_unix_ms is not None,
            self.observed_at_mono_ns is not None,
            self.boot_id is not None,
        )
        if any(supplied) and not all(supplied):
            raise ValueError(
                "intervention snapshot v2 time fields must be supplied together"
            )
        return self


class HelpfulnessRecord(BaseModel):
    """Legacy local product-diagnostic record for one intervention."""
    intervention_id: str = Field(..., description="ID of the evaluated intervention")
    intervention_type: str = Field(..., description="Type of intervention (overlay_only, etc.)")

    # Pre/post state
    pre_state: InterventionSnapshot = Field(..., description="State before intervention")
    post_state: InterventionSnapshot | None = Field(None, description="State after intervention")

    # Timing
    started_at: datetime = Field(
        default_factory=lambda: utc_datetime(SYSTEM_CLOCK)
    )
    ended_at: datetime | None = Field(None)
    time_to_flow_seconds: float | None = Field(
        None, ge=0.0, description="Seconds until user returned to FLOW"
    )

    # Implicit signals
    was_undone: bool = Field(False, description="User clicked undo/restore")
    was_ignored: bool = Field(False, description="Dismissed in <2 seconds")
    was_engaged: bool = Field(False, description="User interacted with intervention")
    interaction_duration_seconds: float = Field(0.0, ge=0.0)

    # Explicit signals
    user_rating: Literal["thumbs_up", "thumbs_down", None] = Field(None)

    # Computed reward
    reward_signal: float = Field(
        0.0, ge=-1.0, le=1.0,
        description="Legacy descriptive score; never causal or production-policy training data"
    )

    # Decode-only compatibility fields from the retired contextual bandit.
    context_features: list[float] = Field(
        default_factory=list,
        description="Legacy feature vector retained only for historical record decoding"
    )
    arm_index: int = Field(0, ge=0, description="Legacy retired-policy arm index")


class BanditWeights(BaseModel):
    """Decode-only weights for the retired LinUCB experiment."""
    n_arms: int = Field(..., ge=1, description="Number of arms (intervention types)")
    n_features: int = Field(..., ge=1, description="Feature dimension")
    # A matrices and b vectors stored as flat lists for JSON serialization
    a_matrices: list[list[float]] = Field(..., description="A matrices (n_arms x n_features x n_features)")
    b_vectors: list[list[float]] = Field(..., description="b vectors (n_arms x n_features)")
    alpha: float = Field(1.0, gt=0.0, description="UCB exploration parameter")
    total_updates: int = Field(0, ge=0, description="Total number of updates")
    arm_labels: list[str] = Field(default_factory=list, description="Human-readable arm names")
