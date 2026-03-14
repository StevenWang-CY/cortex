"""
Cortex Activity Tracking Schemas

Pydantic models for learning activity tracking — receives summaries from
the browser extension and stores daily timelines for handover/briefing.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ActivitySummary(BaseModel):
    """Summary of a single learning activity from the browser."""

    content_id: str = Field(..., description="Canonical URL identifier")
    platform: str = Field(..., description="Platform name (youtube, bilibili, leetcode, etc.)")
    content_type: str = Field(
        ...,
        description="Content type: video, article, code_problem, documentation, "
        "course_lecture, notebook, pdf, slides, general",
    )
    title: str = Field(..., description="Page/video title")
    url: str = Field(..., description="Full URL for navigation")
    position_description: str = Field(
        "", description='Human-readable position, e.g. "32:48 / 1:15:22" or "65% read"'
    )
    duration_spent_s: float = Field(0, ge=0, description="Total accumulated dwell time in seconds")
    last_visited: float = Field(..., description="Epoch milliseconds of last visit")
    completion_pct: float = Field(0, ge=0, le=100, description="Completion percentage")
    topic_tags: list[str] = Field(default_factory=list, description="Auto-extracted topic tags")
    context_snapshot: str = Field("", description="~200 chars of visible text when leaving")


class ActivityTimeline(BaseModel):
    """Daily activity timeline — aggregated from browser extension syncs."""

    date: str = Field(..., description="Date string YYYY-MM-DD")
    activities: list[ActivitySummary] = Field(
        default_factory=list, description="Activities for this date"
    )
    total_learning_s: float = Field(0, ge=0, description="Total learning seconds for the day")
    dominant_topics: list[str] = Field(
        default_factory=list, description="Most frequent topic tags"
    )
