"""
Cortex Intervention Schemas

Pydantic models for intervention plans, workspace snapshots,
and intervention outcomes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def generate_intervention_id() -> str:
    """Generate a unique intervention ID."""
    return f"int_{uuid4().hex[:12]}"


def _generate_action_id() -> str:
    return f"act_{uuid4().hex[:8]}"


class SuggestedAction(BaseModel):
    """A single executable action the user can approve with one click."""

    action_id: str = Field(
        default_factory=_generate_action_id,
        description="Unique action identifier",
    )
    action_type: Literal[
        "close_tab",
        "group_tabs",
        "bookmark_and_close",
        "open_url",
        "search_error",
        "highlight_tab",
        "save_session",
        "copy_to_clipboard",
        "start_timer",
    ] = Field(..., description="Type of executable action")
    tab_index: int | None = Field(
        None,
        description="Integer index referencing the tab list from context (primary ID for tab actions)",
    )
    target: str = Field(
        "",
        max_length=500,
        description="Search query, URL for open_url, session name, etc.",
    )
    label: str = Field(
        ..., max_length=200, description="Human-readable button label"
    )
    reason: str = Field(
        "", max_length=300, description="Why this action helps"
    )
    category: Literal["recommended", "optional", "informational"] = Field(
        "recommended",
        description="How strongly recommended",
    )
    reversible: bool = Field(True, description="Whether this action can be undone")
    group_id: str | None = Field(
        None, description="Groups related actions together"
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Action-specific metadata (tab_title, search_query, etc.)",
    )
    catalog_id: str | None = Field(
        None,
        max_length=80,
        description="Optional curated intervention catalog identifier",
    )


class ErrorAnalysis(BaseModel):
    """LLM analysis of the current error."""

    error_type: str = Field(
        ..., description="Classified error type (syntax, import, type, runtime, etc.)"
    )
    root_cause: str = Field(
        ..., max_length=500, description="Identified root cause"
    )
    suggested_fix: str = Field(
        "", max_length=1000, description="Suggested code fix or approach"
    )
    search_query: str = Field(
        "", max_length=200, description="Pre-crafted search query for this error"
    )
    relevant_doc_url: str = Field(
        "", description="URL to relevant documentation, if identifiable"
    )
    failing_abstraction: str = Field(
        "", max_length=200, description="The specific abstraction or function that is failing"
    )
    symbol_location: str = Field(
        "", max_length=200, description="File:line location of the failing symbol"
    )
    root_cause_category: Literal[
        "type_mismatch", "null_reference", "missing_import", "logic_error",
        "api_misuse", "concurrency", "config", "other"
    ] = Field(
        "other", description="Classified root cause category"
    )
    minimal_edit: str = Field(
        "", max_length=1000, description="Smallest code change that fixes the issue"
    )


class TabRecommendation(BaseModel):
    """LLM recommendation for a single tab."""

    tab_index: int = Field(..., description="Integer index into the context tab list")
    tab_title: str = Field("", description="Tab title for display")
    action: Literal["keep", "close", "group", "bookmark_and_close"] = Field(
        ..., description="Recommended action for this tab"
    )
    reason: str = Field("", max_length=200, description="Why this recommendation")
    relevance_score: float = Field(
        0.5, ge=0.0, le=1.0, description="Relevance to current task"
    )
    group_name: str | None = Field(
        None, description="Group name if action is 'group'"
    )


class TabRecommendations(BaseModel):
    """Complete tab triage from LLM."""

    tabs: list[TabRecommendation] = Field(default_factory=list)
    summary: str = Field(
        "", max_length=300, description="Summary of tab triage reasoning"
    )


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
    # D.6: surfaced here so the VS Code extension can size its fold window
    # without round-tripping the full SimplificationConstraints object.
    # Mirrors SimplificationConstraints.max_visible_lines; the planner
    # populates this from the constraints applied at plan time.
    max_visible_lines: int = Field(
        40,
        ge=10,
        le=400,
        description="Half-window of source lines to keep visible around cursor",
    )


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
    suggested_actions: list[SuggestedAction] = Field(
        default_factory=list, description="Executable actions the user can approve"
    )
    error_analysis: ErrorAnalysis | None = Field(
        None, description="Detailed error analysis with suggested fixes"
    )
    tab_recommendations: TabRecommendations | None = Field(
        None, description="Per-tab keep/close/group recommendations"
    )
    causal_explanation: str = Field(
        "", max_length=500, description="Why Cortex triggered this intervention, referencing specific signals"
    )
    consent_level: Literal[
        "observe", "suggest", "preview", "reversible_act", "autonomous_act"
    ] = Field(
        "suggest", description="Consent ladder level for this intervention"
    )
    plan_warnings: list[str] = Field(
        default_factory=list,
        description="Non-fatal validation or grounding warnings to surface in debug UI",
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
        """Check if plan contains destructive workspace actions (should always be False).

        Uses action_type checking instead of substring matching on labels,
        which avoids false positives on benign labels like 'Close New Tab'.
        close_tab is NOT inherently destructive (it's reversible via undo).
        """
        destructive_action_types = {
            "delete_file", "delete_project", "close_application", "discard_changes",
        }
        for action in self.suggested_actions:
            if action.action_type in destructive_action_types:
                return True
        # close_tab is NOT inherently destructive (it's reversible via undo)
        return False


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
    helpfulness_score: float | None = Field(
        None, ge=-1.0, le=1.0, description="Computed helpfulness reward signal"
    )
    user_rating: Literal["thumbs_up", "thumbs_down", None] = Field(
        None, description="Explicit user rating of intervention"
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
