"""
Cortex Intervention Schemas

Pydantic models for intervention plans, workspace snapshots,
and intervention outcomes.

Also hosts the lightweight ``AdapterCommand`` / ``ValidationResult``
dataclasses that the planner / executor / ports layer share. These live
in libs/ rather than under ``cortex.services.intervention_engine`` so
``cortex.libs.ports.intervention_port`` can reference them without
violating the libs ⊥ services architectural invariant enforced by
``cortex/tests/unit/test_module_boundaries.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Planner-side dataclasses (intentionally NOT pydantic models).
#
# These are runtime carriers between the planner, the executor, and the
# api_gateway port. They never cross the wire (the wire payload is
# ``InterventionPlan`` + ``InterventionApplied``), so a dataclass is the
# right tool — no need for the Pydantic validator machinery. They live in
# libs/ so the InterventionPort Protocol can type-hint them without
# pulling cortex.services back into libs.
# ---------------------------------------------------------------------------


@dataclass
class AdapterCommand:
    """A concrete command to send to a workspace adapter."""

    adapter: str  # "editor", "browser", "terminal", "overlay"
    action: str
    params: dict[str, object] = dc_field(default_factory=dict)


@dataclass
class ValidationResult:
    """Result of plan validation."""

    is_valid: bool
    errors: list[str] = dc_field(default_factory=list)
    warnings: list[str] = dc_field(default_factory=list)

# F10: only these URL schemes may appear in an ``open_url`` action target.
# Excludes ``javascript:``, ``data:``, ``chrome:``, ``file:``, ``vbscript:``
# and other schemes that could trigger code execution or local-file access
# when the extension hands the URL to ``chrome.tabs.create``.
_ALLOWED_URL_SCHEMES: frozenset[str] = frozenset({"http", "https"})

# F10: per-action_type maximum length of the ``target`` string. The
# overall ``max_length=500`` on the field remains as an outer bound;
# these tighter caps catch obvious shape misuse (e.g. a search query
# that is actually a paragraph of malicious instructions).
_TARGET_MAX_LEN: dict[str, int] = {
    "search_error": 200,
    "open_url": 500,
    "copy_to_clipboard": 500,
    "save_session": 200,
    "start_timer": 32,
    "resume_last_active_file": 300,
    "prompt_micro_commit": 80,
    "suggest_movement_break": 32,
    "take_biology_break": 32,
}


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
        "resume_last_active_file",
        "prompt_micro_commit",
        "suggest_movement_break",
        # P0 §3.7: biology-driven break action — launches the full-screen
        # breathing overlay on the desktop shell. Reversible only in the
        # sense that the user can end the session early; nothing
        # destructive happens to the workspace.
        "take_biology_break",
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

    # F10 (audit): executor-safety validators. The LLM cannot be trusted
    # to produce safe arguments; even a well-behaved model can be coaxed
    # into emitting ``open_url`` with a ``javascript:`` URL via prompt
    # injection (F09 closes most of that path, but defence-in-depth
    # requires the executor to refuse unsafe inputs regardless).

    @field_validator("tab_index")
    @classmethod
    def _validate_tab_index(cls, v: int | None) -> int | None:
        if v is None:
            return v
        if v < 0:
            raise ValueError(
                f"tab_index must be non-negative; got {v}. The upper bound "
                "is enforced server-side against the live tab list."
            )
        return v

    @model_validator(mode="after")
    def _validate_target_for_action_type(self) -> SuggestedAction:
        # Per-action_type length cap. The outer ``max_length=500`` field
        # constraint already ran by the time we reach this validator;
        # this is the tighter per-type cap.
        max_len = _TARGET_MAX_LEN.get(self.action_type)
        if max_len is not None and self.target and len(self.target) > max_len:
            raise ValueError(
                f"target too long for action_type={self.action_type!r}: "
                f"{len(self.target)} > {max_len}"
            )

        # ``search_error`` queries must be single-line so they can't smuggle
        # extra instructions into the search box.
        if self.action_type == "search_error" and self.target:
            if "\n" in self.target or "\r" in self.target:
                raise ValueError(
                    "search_error target must not contain line breaks"
                )

        # ``open_url`` target must be a real http(s) URL. Empty target is
        # allowed at parse time (the LLM sometimes emits a placeholder
        # that the enrichment step fills in); a non-empty value with a
        # banned scheme is rejected outright.
        if self.action_type == "open_url" and self.target:
            try:
                parsed = urlparse(self.target)
            except Exception as exc:  # pragma: no cover - urlparse rarely raises
                raise ValueError(
                    f"open_url target is not a parseable URL: {exc}"
                ) from exc
            scheme = (parsed.scheme or "").lower()
            if scheme not in _ALLOWED_URL_SCHEMES:
                raise ValueError(
                    "open_url target must use http or https scheme; got "
                    f"{scheme or '(no scheme)'!r}"
                )
            if not parsed.netloc:
                raise ValueError(
                    "open_url target must include a hostname"
                )
        return self


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


class CausalSignal(BaseModel):
    """P0 §3.9: one ranked driver behind the current state transition.

    The daemon emits a list of these (top 2-3) alongside each
    :class:`InterventionPlan` so the UI can render a structured "Why?"
    drilldown — name, current value, baseline, percent change, and a
    60-sample 1-Hz sparkline buffer. Unlike the legacy free-text
    ``causal_explanation`` string this payload is engine-computed, not
    LLM-composed, and is therefore safe to compare against observable
    values in the F09 verifier.

    Privacy: ``samples_60s`` is the most recent 60 seconds of 1-Hz
    aggregates only; raw frame data never enters this buffer.
    """

    name: str = Field(
        ..., max_length=40, description="Signal label (e.g. 'HRV', 'Tab switches')"
    )
    current_value: float = Field(
        ...,
        description="Latest 1-Hz aggregate observed by the daemon",
    )
    baseline_value: float | None = Field(
        None,
        description="User's personal baseline for this signal (None if unknown)",
    )
    unit: str = Field(
        ..., max_length=10, description="Display unit (ms, bpm, /min, °, …)"
    )
    delta_pct: float | None = Field(
        None, description="Percent change vs baseline; sign carries direction"
    )
    samples_60s: list[float] = Field(
        default_factory=list,
        max_length=60,
        description="Last 60 1-Hz samples for sparkline rendering",
    )
    severity: Literal["primary", "secondary", "tertiary"] = Field(
        "secondary",
        description="Rank of this signal within the explanation (primary first)",
    )


class MicroStep(BaseModel):
    """P0 §3.6: a single micro-step with toggleable completion status.

    Tracks the lifecycle of an intervention's individual next-actions so
    the daemon can persist progress across reconnects and the surfaces
    can render check/strike-through state idempotently.
    """

    text: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Human-readable step text shown to the user",
    )
    status: Literal["pending", "done", "skipped"] = Field(
        "pending",
        description="Current completion status",
    )
    started_at: datetime | None = Field(
        None,
        description="When the user first acted on this step (toggled non-pending)",
    )
    completed_at: datetime | None = Field(
        None,
        description="When the user marked this step done or skipped",
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
    micro_steps: list[MicroStep] = Field(
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
    # C3 (audit): the URL/context of the active tab at trigger time. The
    # daemon stamps this from the active-tab context when building the
    # INTERVENTION_TRIGGER; the browser extension's state-guards read
    # ``plan.trigger_url`` to scope an intervention to the page that
    # provoked it (e.g. suppress a stale overlay after the user has
    # navigated away). ``None`` when the daemon had no active-tab URL to
    # attribute (no browser client connected, or a non-browser trigger).
    trigger_url: str | None = Field(
        None,
        max_length=2048,
        description=(
            "URL of the active tab at trigger time, stamped by the daemon "
            "so surfaces can scope the intervention to its originating "
            "page. None when no active-tab URL was available."
        ),
    )
    # P0 §3.9: structured rationale companion to ``causal_explanation``.
    # Maximum three entries (primary + secondary + tertiary), ranked by
    # |z-score| against the user's baselines. Surfaces in the "Why?"
    # drilldown on every surface. Empty when the daemon could not
    # attribute the trigger (cold-start baselines, missing features).
    causal_signals: list[CausalSignal] = Field(
        default_factory=list,
        max_length=3,
        description="2-3 dominant signals behind the trigger; first is primary",
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
    # F20 / F27 / F29 (audit): non-payload metadata stamped by the
    # daemon. Free-form on purpose so future findings can stash
    # additional non-LLM-controlled hints without bumping the wire
    # schema each time. Known keys:
    #   - ``source`` ∈ {``llm``, ``fallback``} — F27, distinguishes
    #     real-LLM plans from rule-based fallbacks.
    #   - ``fallback_reason`` ∈ {``circuit_open``, ``retries_exhausted``,
    #     ``budget_killed``, ``rule_based``} — F27/F20.
    #   - ``budget_killed`` (bool) — F20, daily-cost kill-switch fired.
    #   - ``context_truncated_sections`` (list[str]) — F29, names of
    #     prompt sections the budget enforcer trimmed.
    # The dismissal-model training pipeline reads ``source`` and skips
    # outcomes from fallback origins so cold-start dismissals don't
    # poison personalisation. Never trust this field for executor
    # decisions — it is purely an observability hint.
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Daemon-stamped plan metadata (e.g. {'source': 'fallback'}) "
            "and prompt-budget telemetry (e.g. "
            "{'context_truncated_sections': ['terminal_errors']}). "
            "Never trust this field for executor decisions — it is "
            "purely an observability hint."
        ),
    )

    @field_validator("micro_steps", mode="before")
    @classmethod
    def _coerce_micro_step_strings(cls, v: Any) -> Any:
        """P0 §3.6: accept legacy list[str] payloads for backward compat.

        Older callers (rule-based fallback planner, LLM JSON responses,
        cached SESSION_RECAP envelopes) emit plain strings. Coerce them
        into MicroStep objects with status='pending' so the same shape
        works for both wire-level and storage-level deserialisation.
        """
        if isinstance(v, list) and v and all(isinstance(item, str) for item in v):
            return [{"text": item, "status": "pending"} for item in v]
        return v

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
        """Get age of dismissal in seconds.

        Robust to a naive OR timezone-aware ``timestamp``. The legacy
        ``datetime.now() - self.timestamp`` raised
        ``TypeError: can't subtract offset-naive and offset-aware
        datetimes`` whenever a caller constructed the record with a
        tz-aware ``timestamp`` (e.g. ``datetime.now(UTC)``), which is the
        shape the rest of Cortex's structured-logging layer emits. We
        compare against ``now`` in the same awareness domain as the
        stored timestamp so the subtraction is always valid.
        """
        if self.timestamp.tzinfo is not None:
            now = datetime.now(self.timestamp.tzinfo)
        else:
            now = datetime.now()
        return (now - self.timestamp).total_seconds()


class InterventionApplied(BaseModel):
    """P0 audit / Phase-4a: client → daemon ack of an intervention apply.

    Wire shape of the ``INTERVENTION_APPLIED`` WS message payload that
    the browser extension (and any other surface that mutates the
    workspace on behalf of the daemon) sends back so the executor can
    overwrite the optimistic ``Mutation.success`` with the real outcome.

    See ``cortex.services.api_gateway.websocket_server._handle_intervention_applied``
    for the dispatch arm that consumes this; the daemon ``_intervention_applied_callback``
    is the runtime handler.
    """

    model_config = ConfigDict(extra="ignore")

    intervention_id: str = Field(
        ..., description="Intervention this ack belongs to"
    )
    phase: Literal["apply", "restore", "execute_action"] = Field(
        "apply",
        description=(
            "Lifecycle phase being acked. 'execute_action' is sent by the "
            "VS Code extension when it acks a non-mutating native action "
            "(e.g. resume_last_active_file) it executed directly."
        ),
    )
    success: bool = Field(
        ...,
        description="True iff every requested mutation succeeded on this client",
    )
    applied_actions: list[str] = Field(
        default_factory=list,
        description="action_ids the client successfully applied",
    )
    errors: list[str] = Field(
        default_factory=list,
        description="Per-action error strings reported by the client",
    )
    source_client_type: str | None = Field(
        None,
        description=(
            "Set by the server-side dispatcher (never trusted from the "
            "wire) so the daemon can attribute the ack to the actual "
            "client_type that produced it."
        ),
    )


class InterventionApplyResult(BaseModel):
    """F05: client-confirmed outcome of an intervention apply.

    The legacy adapter optimistically reported ``success=True`` for every
    dispatched action. The new path waits for an extension-side
    ``INTERVENTION_APPLIED`` ack and surfaces the real outcome via this
    schema. ``confirmed=False`` means either no ack arrived inside the
    timeout window or the client reported a hard failure (no actions
    succeeded). ``applied_actions`` / ``errors`` carry the per-action
    breakdown so partial successes don't masquerade as full successes.
    """

    intervention_id: str = Field(
        ..., description="The intervention whose apply was awaited"
    )
    correlation_id: str | None = Field(
        None, description="Apply-call correlation id (mirrored from the ack)"
    )
    confirmed: bool = Field(
        False,
        description=(
            "True when the extension explicitly acknowledged the apply and "
            "reported success. False on timeout or explicit failure."
        ),
    )
    timed_out: bool = Field(
        False,
        description="True when the watcher resolved the future on timeout",
    )
    applied_actions: list[str] = Field(
        default_factory=list,
        description="action_ids reported as applied by the client",
    )
    errors: list[str] = Field(
        default_factory=list,
        description="Per-action errors reported by the client",
    )
    phase: Literal["apply", "restore", "execute_action"] = Field(
        "apply", description="Which lifecycle phase this result belongs to"
    )
