"""
Cortex Longitudinal Tracking Schemas

Models for tracking baseline drift over days/weeks and building
a chronotype model for dynamic sensitivity adjustment.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class HourlyOverloadRate(BaseModel):
    """Overload rate for a specific hour of day."""
    hour: int = Field(..., ge=0, le=23, description="Hour of day (0-23)")
    overload_rate: float = Field(0.0, ge=0.0, le=1.0, description="Fraction of time in overload")
    sample_count: int = Field(0, ge=0, description="Number of samples for this hour")


class TaskOverloadPattern(BaseModel):
    """Overload pattern for a specific task type or repo."""
    pattern_key: str = Field(..., description="Task type, repo name, or website domain")
    overload_rate: float = Field(0.0, ge=0.0, le=1.0, description="Fraction of time in overload")
    avg_stress_integral: float = Field(0.0, ge=0.0, description="Average stress integral")
    correlation: Literal["trigger", "recovery", "neutral"] = Field(
        "neutral", description="Whether this pattern triggers or aids recovery"
    )


class DailyBaseline(BaseModel):
    """Summary baseline measurements for a single day."""
    record_date: date = Field(..., description="Date of this baseline")
    hr_baseline: float = Field(72.0, ge=40.0, le=120.0, description="Daily mean resting HR")
    hrv_baseline: float = Field(50.0, ge=10.0, le=200.0, description="Daily mean HRV (RMSSD)")
    resp_baseline: float = Field(15.0, ge=4.0, le=30.0, description="Daily mean respiration rate")
    stress_integral_total: float = Field(0.0, ge=0.0, description="Total stress integral for the day")
    stress_integral_threshold: float = Field(500.0, ge=0.0, description="Break threshold used")
    peak_overload_hours: list[int] = Field(
        default_factory=list, description="Hours with highest overload"
    )
    total_flow_minutes: float = Field(0.0, ge=0.0, description="Total minutes in FLOW state")
    total_hyper_minutes: float = Field(0.0, ge=0.0, description="Total minutes in HYPER state")
    session_count: int = Field(0, ge=0, description="Number of work sessions")
    interventions_count: int = Field(0, ge=0, description="Number of interventions triggered")
    interventions_accepted: int = Field(0, ge=0, description="Number of interventions accepted")


class ChronotypeModel(BaseModel):
    """Longitudinal model tracking how baselines drift over weeks."""
    baselines: list[DailyBaseline] = Field(default_factory=list)
    trend_direction: Literal["improving", "stable", "declining"] = Field(
        "stable", description="Overall HRV trend direction"
    )
    sensitivity_multiplier: float = Field(
        1.0, ge=0.5, le=2.0,
        description="Dynamic multiplier for stress integral threshold"
    )
    hourly_patterns: list[HourlyOverloadRate] = Field(
        default_factory=list, description="Per-hour overload patterns"
    )
    task_patterns: list[TaskOverloadPattern] = Field(
        default_factory=list, description="Per-task/repo overload patterns"
    )
    last_updated: datetime | None = Field(None, description="When model was last updated")
    window_days: int = Field(30, ge=7, le=90, description="Number of days in analysis window")
