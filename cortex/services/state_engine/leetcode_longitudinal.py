"""
State Engine — LeetCode Longitudinal Tracker

Extends generic longitudinal tracking with LeetCode-specific metrics:
per-session problem counts, solve times, panic/lockout episodes,
pattern-ladder usage, and per-tag skill growth.

Tracks allostatic load against a daily budget to recommend session endings.
"""

from __future__ import annotations

import logging
from datetime import date as date_type
from typing import Any

from cortex.libs.schemas.leetcode import (
    LeetCodeContext,
    LeetCodeModeEstimate,
    LeetCodeSessionMetrics,
    LeetCodeSkillMetrics,
)

logger = logging.getLogger(__name__)


class LeetCodeLongitudinalTracker:
    """
    Longitudinal tracker specialised for LeetCode coaching sessions.

    Maintains per-session metrics (problems attempted/accepted, panic episodes,
    lockouts, solution escapes, pattern-ladder depth) and per-tag skill metrics
    that persist across sessions.

    The tracker compares current allostatic load against a configurable daily
    budget and signals when the session should end.

    Usage::

        tracker = LeetCodeLongitudinalTracker(daily_load_budget=600.0)
        tracker.record_problem_attempt(leetcode_ctx)
        tracker.record_problem_accepted(leetcode_ctx, time_to_solve_s=180.0)
        if tracker.should_end_session():
            # suggest session end
            ...
    """

    def __init__(self, daily_load_budget: float = 600.0) -> None:
        """Initialise the tracker.

        Args:
            daily_load_budget: Maximum cumulative allostatic load (ms*s) before
                suggesting the user end their session.  Defaults to 600.
        """
        self._daily_load_budget = daily_load_budget
        self._current_load: float = 0.0
        self._session_metrics = LeetCodeSessionMetrics(
            date=date_type.today().isoformat(),
        )
        self._skill_metrics: dict[str, LeetCodeSkillMetrics] = {}

    # ------------------------------------------------------------------
    # Recording helpers
    # ------------------------------------------------------------------

    def record_problem_attempt(self, leetcode_ctx: LeetCodeContext) -> None:
        """Record that the user began a new problem attempt.

        Increments *problems_attempted* and ensures every tag on the problem
        has a corresponding :class:`LeetCodeSkillMetrics` entry.
        """
        self._session_metrics.problems_attempted += 1
        for tag in leetcode_ctx.tags:
            if tag not in self._skill_metrics:
                self._skill_metrics[tag] = LeetCodeSkillMetrics(tag=tag)
            self._skill_metrics[tag].attempts += 1

    def record_problem_accepted(
        self,
        leetcode_ctx: LeetCodeContext,
        time_to_solve_s: float,
    ) -> None:
        """Record a successful acceptance.

        Updates *problems_accepted*, maintains a running average of solve time,
        and increments per-tag accept counts.
        """
        self._session_metrics.problems_accepted += 1

        # Running average for solve time
        prev_avg = self._session_metrics.avg_time_to_solve_s
        accepted = self._session_metrics.problems_accepted
        if prev_avg is None:
            self._session_metrics.avg_time_to_solve_s = time_to_solve_s
        else:
            self._session_metrics.avg_time_to_solve_s = (
                prev_avg * (accepted - 1) + time_to_solve_s
            ) / accepted

        for tag in leetcode_ctx.tags:
            if tag not in self._skill_metrics:
                self._skill_metrics[tag] = LeetCodeSkillMetrics(tag=tag)
            self._skill_metrics[tag].accepts += 1

    def record_panic_episode(self) -> None:
        """Increment the panic-episode counter for the current session."""
        self._session_metrics.panic_episodes += 1

    def record_lockout(self) -> None:
        """Increment the lockout counter for the current session."""
        self._session_metrics.lockout_count += 1

    def record_solution_escape(self) -> None:
        """Increment the solution-escape counter for the current session."""
        self._session_metrics.solution_escape_count += 1

    def record_pattern_ladder_depth(self, depth: int) -> None:
        """Update the maximum pattern-ladder depth reached this session."""
        if depth > self._session_metrics.pattern_ladder_max_depth:
            self._session_metrics.pattern_ladder_max_depth = depth

    # ------------------------------------------------------------------
    # Allostatic load / budget
    # ------------------------------------------------------------------

    def update_load(self, allostatic_load: float) -> None:
        """Update the current allostatic load.

        Also tracks peak load for the session summary.
        Automatically rolls the session over if the date has changed (midnight).
        """
        # Check for midnight rollover
        today = date_type.today().isoformat()
        if self._session_metrics.date != today:
            logger.info(
                "Session date rolled over %s → %s, resetting metrics",
                self._session_metrics.date, today,
            )
            self.reset_session(today)

        self._current_load = allostatic_load
        if allostatic_load > self._session_metrics.peak_allostatic_load:
            self._session_metrics.peak_allostatic_load = allostatic_load

    def record_parasympathetic_window(self) -> None:
        """Record that a parasympathetic learning window was detected."""
        self._session_metrics.parasympathetic_windows += 1

    def should_end_session(self) -> bool:
        """Return ``True`` if allostatic load has exceeded the daily budget."""
        return self._current_load > self._daily_load_budget

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_session_summary(self) -> dict[str, Any]:
        """Return the current session metrics plus per-tag skill metrics."""
        summary = self._session_metrics.model_dump()
        summary["skill_metrics"] = {
            tag: metrics.model_dump()
            for tag, metrics in self._skill_metrics.items()
        }
        return summary

    def get_skill_metrics(self) -> dict[str, LeetCodeSkillMetrics]:
        """Return all per-tag skill metrics."""
        return dict(self._skill_metrics)

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def reset_session(self, date: str) -> None:
        """Reset session metrics for a new day, preserving skill metrics.

        Args:
            date: The new session date in ``YYYY-MM-DD`` format.
        """
        self._session_metrics = LeetCodeSessionMetrics(date=date)
        self._current_load = 0.0

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def session_metrics(self) -> LeetCodeSessionMetrics:
        """Current session metrics."""
        return self._session_metrics

    @property
    def budget_remaining(self) -> float:
        """Allostatic-load budget remaining (daily_load_budget - current load)."""
        return self._daily_load_budget - self._current_load

    @property
    def budget_ratio(self) -> float:
        """Fraction of the daily load budget consumed (current / budget)."""
        if self._daily_load_budget <= 0:
            return 1.0
        return self._current_load / self._daily_load_budget

    # ------------------------------------------------------------------
    # Serialisation (for Redis persistence)
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Serialise tracker state to a plain dict for Redis persistence."""
        return {
            "daily_load_budget": self._daily_load_budget,
            "current_load": self._current_load,
            "session_metrics": self._session_metrics.model_dump(),
            "skill_metrics": {
                tag: metrics.model_dump()
                for tag, metrics in self._skill_metrics.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> LeetCodeLongitudinalTracker:
        """Reconstruct a tracker from a previously serialised dict."""
        tracker = cls(daily_load_budget=data.get("daily_load_budget", 600.0))
        tracker._current_load = data.get("current_load", 0.0)
        tracker._session_metrics = LeetCodeSessionMetrics(
            **data.get("session_metrics", {"date": date_type.today().isoformat()}),
        )
        for tag, metrics_data in data.get("skill_metrics", {}).items():
            tracker._skill_metrics[tag] = LeetCodeSkillMetrics(**metrics_data)
        return tracker
