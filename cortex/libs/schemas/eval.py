"""
Cortex Evaluation Schemas

Models for tracking intervention helpfulness and contextual bandit learning.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class InterventionSnapshot(BaseModel):
    """Workspace state snapshot for pre/post comparison."""
    state: str = Field(..., description="User state (FLOW/HYPER/HYPO/RECOVERY)")
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    complexity_score: float = Field(0.0, ge=0.0, le=1.0)
    tab_count: int = Field(0, ge=0)
    error_count: int = Field(0, ge=0)
    thrashing_score: float = Field(0.0, ge=0.0, le=1.0)
    stress_integral: float = Field(0.0, ge=0.0)
    timestamp: float = Field(0.0)


class HelpfulnessRecord(BaseModel):
    """Complete evaluation record for a single intervention."""
    intervention_id: str = Field(..., description="ID of the evaluated intervention")
    intervention_type: str = Field(..., description="Type of intervention (overlay_only, etc.)")

    # Pre/post state
    pre_state: InterventionSnapshot = Field(..., description="State before intervention")
    post_state: InterventionSnapshot | None = Field(None, description="State after intervention")

    # Timing
    started_at: datetime = Field(default_factory=datetime.now)
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
        description="Computed reward signal for bandit learning"
    )

    # Context features for bandit
    context_features: list[float] = Field(
        default_factory=list,
        description="Feature vector for contextual bandit [state, complexity, tabs, errors, hour, thrashing, stress, consent]"
    )
    arm_index: int = Field(0, ge=0, description="Bandit arm index that was selected")


class BanditWeights(BaseModel):
    """Persisted weights for the LinUCB contextual bandit."""
    n_arms: int = Field(..., ge=1, description="Number of arms (intervention types)")
    n_features: int = Field(..., ge=1, description="Feature dimension")
    # A matrices and b vectors stored as flat lists for JSON serialization
    a_matrices: list[list[float]] = Field(..., description="A matrices (n_arms x n_features x n_features)")
    b_vectors: list[list[float]] = Field(..., description="b vectors (n_arms x n_features)")
    alpha: float = Field(1.0, gt=0.0, description="UCB exploration parameter")
    total_updates: int = Field(0, ge=0, description="Total number of updates")
    arm_labels: list[str] = Field(default_factory=list, description="Human-readable arm names")
