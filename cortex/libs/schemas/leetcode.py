"""
Cortex LeetCode Domain Schemas

Domain-specific types for the LeetCode competitive programming coach.
Cross-references DOM-derived problem stage with biological mode to deliver
learning-preserving interventions.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class LeetCodeStage(StrEnum):
    """Problem-solving stage derived from DOM observation."""

    READ = "READ"          # Reading/understanding the problem statement
    PLAN = "PLAN"          # Planning approach, writing pseudo-code
    IMPLEMENT = "IMPLEMENT"  # Active coding, high typing rate
    DEBUG = "DEBUG"        # Post-submission with Wrong Answer / error visible
    REFLECT = "REFLECT"    # Post-submission with Accepted visible


class LeetCodeMode(StrEnum):
    """Biological mode specific to competitive programming context."""

    FLOW = "FLOW"                          # Optimal engagement
    PRODUCTIVE_STRUGGLE = "PRODUCTIVE_STRUGGLE"  # Healthy challenge, learning happening
    DESTRUCTIVE_STRUGGLE = "DESTRUCTIVE_STRUGGLE"  # Thrashing without progress
    PANIC = "PANIC"                        # Fight-or-flight, spray-and-pray
    FATIGUE = "FATIGUE"                    # Allostatic load exceeded budget
    AMYGDALA_HIJACK = "AMYGDALA_HIJACK"    # Acute emotional spike post-WA


class SubmissionResult(StrEnum):
    """LeetCode submission outcome types."""

    ACCEPTED = "Accepted"
    WRONG_ANSWER = "Wrong Answer"
    RUNTIME_ERROR = "Runtime Error"
    TIME_LIMIT_EXCEEDED = "Time Limit Exceeded"
    MEMORY_LIMIT_EXCEEDED = "Memory Limit Exceeded"
    COMPILE_ERROR = "Compile Error"


class LeetCodeContext(BaseModel):
    """
    Context captured from LeetCode DOM and code telemetry.

    Updated at 1Hz by the DOM observer content script.
    """

    # Problem metadata
    problem_id: str | None = Field(None, description="LeetCode problem ID")
    title: str = Field("", description="Problem title")
    difficulty: str = Field("", description="Easy / Medium / Hard")
    tags: list[str] = Field(default_factory=list, description="Problem topic tags (DP, Graph, etc.)")

    # Session progress
    time_elapsed_s: float = Field(0.0, ge=0.0, description="Seconds since problem opened")
    submission_count: int = Field(0, ge=0, description="Total submissions this problem")
    wrong_answer_count: int = Field(0, ge=0, description="Wrong Answer count this problem")
    last_submission_result: SubmissionResult | None = Field(
        None, description="Most recent submission result"
    )
    last_submission_ts: float | None = Field(
        None, description="Timestamp of last submission"
    )
    accepted: bool = Field(False, description="Whether problem has been accepted")

    # Stage inference
    stage: LeetCodeStage = Field(
        LeetCodeStage.READ, description="Current problem-solving stage"
    )

    # Code telemetry
    code_snapshot: str = Field("", description="Current editor code content")
    code_line_count: int = Field(0, ge=0, description="Lines of code in editor")
    code_delete_ratio_60s: float = Field(
        0.0, ge=0.0, le=1.0,
        description="Ratio of chars deleted / chars typed in last 60s window"
    )
    chars_per_min: float = Field(0.0, ge=0.0, description="Typing rate in chars/min")

    # Behavioral signals
    reread_count: int = Field(
        0, ge=0, description="Times user scrolled back to problem description"
    )
    solutions_tab_attempted: bool = Field(
        False, description="Whether user tried to open Solutions/Editorial tab"
    )


class DestructiveStruggleEstimate(BaseModel):
    """Output from the destructive struggle detector."""

    is_destructive: bool = Field(False, description="Whether destructive struggle is detected")
    pathway: str = Field(
        "", description="Detection pathway: 'comprehension' or 'implementation'"
    )
    confidence: float = Field(0.0, ge=0.0, le=1.0, description="Detection confidence")


class LeetCodeModeEstimate(BaseModel):
    """
    Combined stage + mode estimate for intervention matrix lookup.

    Produced by LeetCodeModeResolver from generic StateEstimate + LeetCodeContext.
    """

    mode: LeetCodeMode = Field(..., description="Current biological mode")
    stage: LeetCodeStage = Field(..., description="Current problem-solving stage")
    confidence: float = Field(0.0, ge=0.0, le=1.0, description="Estimate confidence")
    aai_score: float = Field(0.0, ge=0.0, description="Amygdala Hijack Index score")
    allostatic_load: float = Field(0.0, ge=0.0, description="Current allostatic load")
    parasympathetic_rebound: bool = Field(
        False, description="Whether parasympathetic rebound detected (learning window)"
    )
    destructive: DestructiveStruggleEstimate = Field(
        default_factory=DestructiveStruggleEstimate,
        description="Destructive struggle estimate"
    )

    @property
    def stage_mode_pair(self) -> tuple[LeetCodeStage, LeetCodeMode]:
        """Get the (stage, mode) cell for intervention matrix lookup."""
        return (self.stage, self.mode)

    @property
    def is_learning_window(self) -> bool:
        """Check if this is an optimal learning window (post-accept + rebound)."""
        return self.parasympathetic_rebound and self.stage == LeetCodeStage.REFLECT


class PatternLadderRung(BaseModel):
    """A single rung in the progressive hint ladder."""

    level: int = Field(..., ge=1, le=4, description="Rung level (1=category, 4=pseudocode)")
    label: str = Field(..., description="Rung label (e.g. 'Category', 'Key Insight')")
    content: str = Field(..., description="Hint content for this rung")
    revealed: bool = Field(False, description="Whether user has revealed this rung")
    revealed_at: float | None = Field(None, description="Timestamp when revealed")


class LeetCodeSessionMetrics(BaseModel):
    """Per-session metrics for longitudinal tracking."""

    date: str = Field(..., description="Session date (YYYY-MM-DD)")
    problems_attempted: int = Field(0, ge=0)
    problems_accepted: int = Field(0, ge=0)
    total_time_s: float = Field(0.0, ge=0.0)
    avg_time_to_solve_s: float | None = Field(None, ge=0.0)
    panic_episodes: int = Field(0, ge=0)
    lockout_count: int = Field(0, ge=0)
    solution_escape_count: int = Field(0, ge=0)
    pattern_ladder_max_depth: int = Field(0, ge=0, le=4)
    peak_allostatic_load: float = Field(0.0, ge=0.0)
    parasympathetic_windows: int = Field(0, ge=0)


class LeetCodeSkillMetrics(BaseModel):
    """Per-tag skill tracking for longitudinal growth."""

    tag: str = Field(..., description="Problem tag (e.g. 'Dynamic Programming')")
    attempts: int = Field(0, ge=0)
    accepts: int = Field(0, ge=0)
    avg_bio_state_during_solve: str = Field(
        "", description="Most common LeetCodeMode during successful solves"
    )

    @property
    def acceptance_rate(self) -> float:
        """Compute acceptance rate for this tag."""
        return self.accepts / self.attempts if self.attempts > 0 else 0.0
