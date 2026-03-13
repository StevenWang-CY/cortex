"""
Cortex Consent Ladder Schemas

Models for the formalized consent hierarchy that governs
how aggressively Cortex can act on the workspace.
"""

from __future__ import annotations

from datetime import datetime
from enum import IntEnum

from pydantic import BaseModel, Field


class ConsentLevel(IntEnum):
    """Consent levels from least to most autonomous."""
    OBSERVE = 0       # Only observe and log
    SUGGEST = 1       # Show popup/overlay suggestion
    PREVIEW = 2       # Show preview of proposed changes
    REVERSIBLE_ACT = 3  # Execute reversible workspace changes
    AUTONOMOUS_ACT = 4  # Execute without preview (earned trust)


class ConsentRecord(BaseModel):
    """Record of a user consent decision."""
    action_type: str = Field(..., description="Type of action (close_tab, fold_code, etc.)")
    level: int = Field(..., ge=0, le=4, description="ConsentLevel value")
    approved: bool = Field(..., description="Whether user approved")
    timestamp: datetime = Field(default_factory=datetime.now, description="When decision was made")


class ActionConsentState(BaseModel):
    """Consent state for a specific action type."""
    action_type: str = Field(..., description="Type of action")
    current_level: int = Field(
        ConsentLevel.SUGGEST, ge=0, le=4, description="Current consent level"
    )
    approval_count: int = Field(0, ge=0, description="Total approvals at current level")
    rejection_count: int = Field(0, ge=0, description="Total rejections at current level")
    escalation_threshold: int = Field(
        5, ge=1, description="Approvals needed to escalate"
    )
    last_approval: datetime | None = Field(None, description="When last approved")
    last_rejection: datetime | None = Field(None, description="When last rejected")


class ConsentLadderState(BaseModel):
    """Complete consent ladder state for all action types."""
    action_states: dict[str, ActionConsentState] = Field(
        default_factory=dict,
        description="Per-action-type consent states"
    )
    global_max_level: int = Field(
        ConsentLevel.REVERSIBLE_ACT, ge=0, le=4,
        description="Global maximum consent level (user-configurable cap)"
    )
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class ConsentDecision(BaseModel):
    """Result of a consent check."""
    allowed: bool = Field(..., description="Whether the action is allowed")
    effective_level: int = Field(..., ge=0, le=4, description="Level the action will execute at")
    requested_level: int = Field(..., ge=0, le=4, description="Level that was requested")
    reason: str = Field("", description="Explanation for the decision")
