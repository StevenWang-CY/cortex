"""
Cortex Intervention Schemas

Pydantic models for intervention plans, workspace snapshots,
and intervention outcomes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def generate_intervention_id() -> str:
    """Generate a unique intervention ID."""
    return f"int_{uuid4().hex[:12]}"


class UIPlan(BaseModel):
    """UI manipulation plan from LLM."""

    dim_background: bool = Field(
        False, description="Whether to dim background windows"
    )
    show_overlay: bool = Field(
        True, description="Whether to show intervention overlay"
    )
    fold_unrelated_code: bool = Field(
        False, description="Whether to fold unrelated code in editor"
    )
    intervention_type: Literal[
        "overlay_only", "simplified_workspace", "guided_mode"
    ] = Field("overlay_only", description="Type of intervention")


class SimplificationConstraints(BaseModel):
    """Constraints for workspace simplification."""

    max_visible_tabs: int = Field(
        3, ge=1, le=10, description="Maximum visible browser tabs"
    )
    max_visible_lines: int = Field(
        50, ge=10, le=200, description="Maximum visible code lines"
    )
    fold_all_except_current: bool = Field(
        True, description="Fold all code except current function"
    )
    hide_terminal_history: bool = Field(
        False, description="Hide terminal output except errors"
    )
    preserve_active_tab: bool = Field(
        True, description="Always keep active tab visible"
    )


class InterventionPlan(BaseModel):
    """
    Complete intervention plan from LLM engine.

    This is the structured output the LLM produces, which is then
    validated and executed by the intervention engine.
    """

    intervention_id: str = Field(
        default_factory=generate_intervention_id,
        description="Unique intervention identifier",
    )
    level: Literal["overlay_only", "simplified_workspace", "guided_mode"] = Field(
        ..., description="Intervention severity level"
    )
    situation_summary: str = Field(
        ..., max_length=500, description="1-2 sentence summary of situation"
    )
    headline: str = Field(
        ..., max_length=100, description="Headline for overlay (< 15 words)"
    )
    primary_focus: str = Field(
        ..., max_length=200, description="The one thing to focus on"
    )
    micro_steps: list[str] = Field(
        ..., min_length=1, max_length=3, description="1-3 concrete next steps"
    )
    hide_targets: list[str] = Field(
        default_factory=list, description="Elements to hide/fold"
    )
    ui_plan: UIPlan = Field(..., description="UI manipulation instructions")
    tone: Literal["direct", "supportive", "minimal"] = Field(
        "direct", description="Tone of intervention text"
    )

    @property
    def is_valid(self) -> bool:
        """Validate intervention plan constraints."""
        if len(self.headline.split()) > 15:
            return False
        if len(self.micro_steps) < 1 or len(self.micro_steps) > 3:
            return False
        if not self.situation_summary or not self.primary_focus:
            return False
        return True

    @property
    def is_destructive(self) -> bool:
        """Check if plan contains destructive actions (should always be False)."""
        destructive_keywords = ["delete", "close", "remove permanently", "discard"]
        all_text = " ".join([
            self.situation_summary,
            self.headline,
            *self.micro_steps,
            *self.hide_targets,
        ]).lower()
        return any(kw in all_text for kw in destructive_keywords)


class FoldState(BaseModel):
    """Editor fold state snapshot."""

    file_path: str = Field(..., description="File path")
    folded_ranges: list[tuple[int, int]] = Field(
        default_factory=list, description="List of folded line ranges"
    )


class TabVisibility(BaseModel):
    """Browser tab visibility state."""

    tab_id: str = Field(..., description="Tab identifier")
    url: str = Field(..., description="Tab URL")
    was_visible: bool = Field(..., description="Whether tab was visible before")
    was_active: bool = Field(..., description="Whether tab was active before")


class WorkspaceSnapshot(BaseModel):
    """
    Pre-intervention workspace state for restoration.

    Captured before any mutations to allow full restoration.
    """

    intervention_id: str = Field(..., description="Associated intervention ID")
    timestamp: float = Field(..., description="When snapshot was taken")

    # Editor state
    fold_states: list[FoldState] = Field(
        default_factory=list, description="Editor fold states"
    )
    editor_visible_range: tuple[int, int] | None = Field(
        None, description="Editor visible range before intervention"
    )

    # Browser state
    tab_visibility: list[TabVisibility] = Field(
        default_factory=list, description="Tab visibility states"
    )
    active_tab_id: str | None = Field(
        None, description="ID of active tab before intervention"
    )

    # Overlay state
    overlay_present: bool = Field(
        False, description="Whether overlay was already showing"
    )

    # Terminal state
    terminal_scroll_position: int | None = Field(
        None, description="Terminal scroll position"
    )

    @property
    def has_editor_state(self) -> bool:
        """Check if editor state was captured."""
        return len(self.fold_states) > 0 or self.editor_visible_range is not None

    @property
    def has_browser_state(self) -> bool:
        """Check if browser state was captured."""
        return len(self.tab_visibility) > 0


class InterventionOutcome(BaseModel):
    """
    Outcome tracking for an intervention.

    Records what happened after intervention was applied.
    """

    intervention_id: str = Field(..., description="Associated intervention ID")
    started_at: datetime = Field(..., description="When intervention started")
    ended_at: datetime | None = Field(None, description="When intervention ended")
    duration_seconds: float | None = Field(
        None, ge=0.0, description="Duration of intervention"
    )

    user_action: Literal[
        "dismissed",  # User clicked dismiss or pressed Escape
        "engaged",  # User interacted with intervention content
        "snoozed",  # User requested snooze
        "timed_out",  # Intervention auto-expired
        "natural_recovery",  # User naturally returned to FLOW
        "system_cancelled",  # System cancelled intervention
    ] = Field(..., description="How intervention ended")

    recovery_detected: bool = Field(
        False, description="Whether recovery was detected post-intervention"
    )
    recovery_confidence: float | None = Field(
        None, ge=0.0, le=1.0, description="Confidence of recovery detection"
    )
    workspace_restored: bool = Field(
        False, description="Whether workspace was restored"
    )
    restore_errors: list[str] = Field(
        default_factory=list, description="Errors during restoration"
    )

    @property
    def was_successful(self) -> bool:
        """Check if intervention led to recovery."""
        return (
            self.user_action in ("engaged", "natural_recovery")
            and self.recovery_detected
        )

    @property
    def was_rejected(self) -> bool:
        """Check if user rejected the intervention."""
        return self.user_action == "dismissed"


class DismissalRecord(BaseModel):
    """Record of an intervention dismissal for adaptive learning."""

    intervention_id: str = Field(..., description="Dismissed intervention ID")
    timestamp: datetime = Field(..., description="When dismissal occurred")
    state_at_dismissal: str = Field(..., description="User state when dismissed")
    confidence_at_dismissal: float = Field(
        ..., ge=0.0, le=1.0, description="Confidence when dismissed"
    )

    @property
    def age_seconds(self) -> float:
        """Get age of dismissal in seconds."""
        return (datetime.now() - self.timestamp).total_seconds()
