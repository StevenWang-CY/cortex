"""Session Report — data models (canonical location).

This module owns the on-disk ``SessionReport`` schema. The legacy import
path ``cortex.services.session_report.models`` is preserved as a thin
re-export shim (see Phase 4.A P0 §3.1/§3.2/§3.3 schema relocation) so
existing call sites keep working while the codegen pipeline sees a
single source of truth under ``cortex.libs.schemas``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class StateTransition(BaseModel):
    """A single state transition event."""

    from_state: str
    to_state: str
    timestamp: datetime


class ActivitySummary(BaseModel):
    """Summary of a single activity during the session."""

    title: str
    tab_type: str = "other"
    dwell_seconds: float = 0.0
    state_breakdown: dict[str, float] = Field(default_factory=dict)


class BreakRecord(BaseModel):
    """P0 §3.7: a single biology-driven break event.

    Captured by :class:`cortex.services.intervention_engine.break_overlay.BiologyBreakController`
    when a guided breathing session starts; ``pre_hrv`` is the most
    recent HRV reading before the overlay shows, ``post_hrv`` is the
    reading captured on exit (natural completion or early termination),
    and ``recovery_delta = post_hrv - pre_hrv``. ``completed`` is True
    only when the breathing pattern ran the full ``duration_seconds``;
    early termination still preserves the record for the reward signal.
    """

    started_at: datetime = Field(..., description="UTC timestamp when the break began")
    duration_seconds: float = Field(
        ..., ge=0.0, description="Wall-clock seconds the break ran (≤ requested)"
    )
    pattern: Literal["box", "4-7-8", "coherent"] = Field(
        ..., description="Breathing pattern that paced the session"
    )
    pre_hrv: float | None = Field(
        None, description="HRV (RMSSD) immediately before the break began"
    )
    post_hrv: float | None = Field(
        None, description="HRV (RMSSD) immediately after the break ended"
    )
    recovery_delta: float | None = Field(
        None, description="post_hrv - pre_hrv (positive = recovery)"
    )
    completed: bool = Field(
        True, description="True if the breathing pattern ran the full duration"
    )
    audio_cue: bool = Field(
        True, description="Whether the audio chime accompanied the breathing"
    )
    reason: str = Field(
        "", max_length=120, description="Why the break was recommended"
    )


class ComparisonStats(BaseModel):
    """Comparison to 7-day rolling averages."""

    focus_delta: float = 0.0  # percentage points
    stress_delta: float = 0.0
    duration_delta: float = 0.0  # seconds



class SessionReport(BaseModel):
    """Biometric study session report."""

    # Phase-4a Debt-1 closure: per-document schema version stamped onto
    # every on-disk session_<id>.json. Readers default to 1 for legacy
    # documents written before this field existed; future schema bumps
    # bump the integer + add migration logic in
    # ``session_report.reader``. The reader emits a ``log.warning`` when
    # input is missing the field so silent drift is visible.
    schema_version: int = Field(
        default=1,
        ge=1,
        description=(
            "Document schema version. Bump on any non-backward-"
            "compatible field rename / removal. Readers SHOULD log a "
            "warning when a document is missing this key so we can "
            "track legacy-write rate."
        ),
    )

    session_id: str
    start_time: datetime
    end_time: datetime
    duration_seconds: float

    # P0 §3.13: optional user-provided session goal. The desktop shell
    # emits ``GOAL_SET`` when the user types into the dashboard goal
    # input or picks a recent goal; the daemon stamps the most-recent
    # value here when ``finish()`` runs so longitudinal aggregators
    # (cortex/services/session_report/longitudinal.py) can later detect
    # "task_overload_pattern" trends.
    goal_title: str | None = Field(
        None,
        max_length=240,
        description=(
            "User-provided session goal (trimmed). None when the user "
            "never set a goal during the session."
        ),
    )

    # Biometric summary
    time_in_flow_seconds: float = 0.0
    time_in_hyper_seconds: float = 0.0
    time_in_hypo_seconds: float = 0.0
    time_in_recovery_seconds: float = 0.0
    flow_percentage: float = 0.0  # biometrically-verified focus vs wall-clock
    longest_flow_streak_seconds: float = 0.0

    # Stress
    peak_stress_integral: float = 0.0
    breaks_taken: int = 0
    breaks_recommended: int = 0
    # P1 Pipeline B: real intervention counters. ``interventions_triggered``
    # increments on every accepted plan (anti-spam pipeline approved it);
    # ``interventions_accepted`` increments only when the user kept the
    # change (thumbs-up / "Keep" outcome). Longitudinal roll-ups read these
    # straight, instead of aliasing both to the HYPER-state transition count.
    interventions_triggered: int = 0
    interventions_accepted: int = 0
    # P0 §3.7: per-break audit trail. Each entry is one guided
    # breathing session; ``recovery_delta`` is the headline number
    # surfaced on the session report card.
    break_records: list[BreakRecord] = Field(default_factory=list)

    # Activity
    state_transitions: list[StateTransition] = Field(default_factory=list)
    top_activities: list[ActivitySummary] = Field(default_factory=list)
    top_distraction_domains: list[str] = Field(default_factory=list)

    # Insights
    golden_hour_start: int | None = None  # hour of day with highest flow ratio
    golden_hour_end: int | None = None
    avg_hr_bpm: float | None = None
    avg_hrv_rmssd: float | None = None
    comparison_to_7day: ComparisonStats | None = None


__all__ = [
    "StateTransition",
    "ActivitySummary",
    "BreakRecord",
    "ComparisonStats",
    "SessionReport",
]
