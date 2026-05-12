"""
State Engine — Anti-Rabbit Hole Circuit Breaker

Detects mismatch between the session goal and the active context.
E.g., user goal is "Build core A* search" but they've spent 45 minutes
in ui_animations.ts in a FLOW state.

When detected, Cortex brings the goal-relevant file forward and shows
a reversible prompt.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Detection thresholds
_MIN_DRIFT_MINUTES = 10.0  # Must be off-task for 10+ minutes
_ALIGNMENT_THRESHOLD = 0.3  # Below this = off-task
_COOLDOWN_SECONDS = 600.0   # 10 min between triggers


@dataclass(frozen=True)
class RabbitHoleAlert:
    """Alert when user has drifted from their session goal."""
    goal: str
    current_file: str
    current_app: str
    drift_minutes: float
    alignment_score: float
    suggested_file: str | None
    summary: str


class RabbitHoleDetector:
    """
    Detects goal-context mismatch (rabbit-hole drift).

    Compares the session's focus_goal keywords against the current
    active file, tab titles, and window titles. If alignment stays
    below threshold for too long, triggers a circuit breaker.

    Usage:
        detector = RabbitHoleDetector()
        alert = detector.check(
            goal="implement A* search",
            current_file="ui_animations.ts",
            current_app="Code",
            tab_titles=["..."],
            state="FLOW",
            current_time=now,
        )
        if alert:
            bring_goal_file_forward(alert.suggested_file)
    """

    def __init__(
        self,
        min_drift_minutes: float = _MIN_DRIFT_MINUTES,
        alignment_threshold: float = _ALIGNMENT_THRESHOLD,
        cooldown_seconds: float = _COOLDOWN_SECONDS,
    ) -> None:
        self._min_drift_minutes = min_drift_minutes
        self._alignment_threshold = alignment_threshold
        self._cooldown = cooldown_seconds

        self._drift_start: float | None = None
        self._last_trigger: float = 0.0
        self._last_goal: str = ""
        self._goal_keywords: list[str] = []
        self._goal_files: list[str] = []  # Files seen when on-task

    def set_goal(self, goal: str) -> None:
        """Set or update the session goal."""
        self._last_goal = goal
        self._goal_keywords = self._extract_keywords(goal)
        self._drift_start = None

    def _extract_keywords(self, goal: str) -> list[str]:
        """Extract meaningful keywords from goal string."""
        stop_words = {
            "a", "an", "the", "is", "are", "was", "were", "be", "been",
            "do", "does", "did", "have", "has", "had", "will", "would",
            "could", "should", "may", "might", "can", "shall",
            "and", "or", "but", "if", "then", "else", "when", "where",
            "how", "what", "which", "who", "whom", "why",
            "in", "on", "at", "to", "for", "of", "with", "by", "from",
            "up", "out", "off", "over", "under", "again", "further",
            "my", "your", "our", "their", "its", "i", "me", "we", "you",
            "it", "they", "them", "this", "that", "these", "those",
            "not", "no", "nor", "so", "too", "very",
            "core", "main", "basic", "simple",
        }
        # Short technical terms that should be kept as keywords
        tech_short = {
            "go", "ml", "ai", "css", "sql", "vue", "rx", "aws", "gcp", "api",
            "cli", "gui", "dom", "npm", "pip", "git", "ux", "ui", "db",
            "os", "ci", "cd", "qa", "c++", "c#", "r", "dx", "io", "jwt",
        }
        words = goal.lower().replace("-", " ").replace("_", " ").split()
        keywords = [
            w for w in words
            if w not in stop_words and (len(w) > 2 or w in tech_short)
        ]
        return keywords

    def record_on_task_file(self, file_path: str) -> None:
        """Record a file that was being worked on while on-task."""
        if file_path and file_path not in self._goal_files:
            self._goal_files.append(file_path)
            # Keep only last 10
            if len(self._goal_files) > 10:
                self._goal_files = self._goal_files[-10:]

    def check(
        self,
        goal: str,
        current_file: str,
        current_app: str,
        tab_titles: list[str] | None = None,
        state: str = "FLOW",
        current_time: float | None = None,
    ) -> RabbitHoleAlert | None:
        """
        Check for goal-context mismatch.

        Args:
            goal: Session focus goal string.
            current_file: Currently active file path/name.
            current_app: Currently active application name.
            tab_titles: List of open tab/window titles.
            state: Current cognitive state.
            current_time: Override timestamp.

        Returns:
            RabbitHoleAlert if drift exceeds threshold, None otherwise.
        """
        if current_time is None:
            current_time = time.monotonic()

        # Need a goal to detect drift
        if not goal:
            return None

        # Update goal if changed
        if goal != self._last_goal:
            self.set_goal(goal)

        # Check cooldown
        if current_time - self._last_trigger < self._cooldown:
            return None

        # Only trigger in FLOW or HYPO (in HYPER, user is already struggling)
        if state not in ("FLOW", "HYPO"):
            self._drift_start = None
            return None

        # Compute alignment
        alignment = self._compute_alignment(
            current_file, current_app, tab_titles or [],
        )

        if alignment >= self._alignment_threshold:
            # On-task: reset drift and record file
            self._drift_start = None
            if current_file:
                self.record_on_task_file(current_file)
            return None

        # Off-task: start or continue drift tracking
        if self._drift_start is None:
            self._drift_start = current_time
            return None

        drift_minutes = (current_time - self._drift_start) / 60.0
        if drift_minutes < self._min_drift_minutes:
            return None

        # Trigger!
        self._last_trigger = current_time
        self._drift_start = None

        # Suggest the most recently recorded on-task file
        suggested = self._goal_files[-1] if self._goal_files else None

        current_name = current_file.split("/")[-1] if "/" in current_file else current_file

        alert = RabbitHoleAlert(
            goal=goal,
            current_file=current_file,
            current_app=current_app,
            drift_minutes=drift_minutes,
            alignment_score=alignment,
            suggested_file=suggested,
            summary=(
                f"You've been in {current_name} for {drift_minutes:.0f} minutes, "
                f"but your goal is \"{goal}\". "
                f"Alignment score: {alignment:.0%}."
            ),
        )

        logger.info(
            "Rabbit hole detected: goal=%r, current=%s, drift=%.1fmin, alignment=%.2f",
            goal, current_file, drift_minutes, alignment,
        )
        return alert

    def _compute_alignment(
        self,
        current_file: str,
        current_app: str,
        tab_titles: list[str],
    ) -> float:
        """
        Compute alignment between current context and goal keywords.

        Uses keyword overlap across file name, app name, and tab titles.
        """
        if not self._goal_keywords:
            return 1.0

        # Build context string from all sources
        context_parts = [
            current_file.lower(),
            current_app.lower(),
        ]
        context_parts.extend(t.lower() for t in tab_titles[:10])
        context_text = " ".join(context_parts)
        context_text = context_text.replace("-", " ").replace("_", " ").replace(".", " ")

        # Count keyword matches
        matches = sum(1 for kw in self._goal_keywords if kw in context_text)
        if not self._goal_keywords:
            return 1.0

        return matches / len(self._goal_keywords)

    @property
    def is_drifting(self) -> bool:
        """Whether drift is currently being tracked."""
        return self._drift_start is not None

    @property
    def drift_duration_minutes(self) -> float:
        """Current drift duration in minutes."""
        if self._drift_start is None:
            return 0.0
        return (time.monotonic() - self._drift_start) / 60.0
