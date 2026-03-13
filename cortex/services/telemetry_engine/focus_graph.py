"""
Telemetry Engine — Focus Transition Graph (Thrashing Detection)

Builds a lightweight directed graph where:
- Nodes = App/Tab titles (deduplicated by app_name + title hash)
- Edges = Switch events
- Weights = Dwell time

Thrashing is detected when the user has high window-switching velocity
across 3-4+ specific nodes in under 60 seconds (e.g., bouncing between
Terminal, auth.ts, and AWS Docs).

The graph also computes an alignment score against the session goal
to detect rabbit-hole drift.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Thrashing detection parameters
_DEFAULT_WINDOW_SECONDS = 60.0
_MIN_NODES_FOR_THRASHING = 3
_MAX_DWELL_FOR_THRASHING_MS = 15000.0  # 15 seconds
_THRASHING_VELOCITY_THRESHOLD = 0.5  # switches per second


@dataclass
class _GraphNode:
    """Internal node representation."""
    node_id: str
    app_name: str
    window_title: str
    total_dwell_ms: float = 0.0
    visit_count: int = 0
    last_enter_ts: float = 0.0


@dataclass
class _GraphEdge:
    """Internal edge representation."""
    from_id: str
    to_id: str
    count: int = 0
    total_dwell_before_ms: float = 0.0
    last_ts: float = 0.0

    @property
    def mean_dwell_ms(self) -> float:
        return self.total_dwell_before_ms / self.count if self.count > 0 else 0.0


@dataclass
class _FocusEvent:
    """A recorded focus change event."""
    node_id: str
    app_name: str
    window_title: str
    timestamp: float


class FocusGraphBuilder:
    """
    Builds and maintains a focus transition graph for thrashing detection.

    Consumes window focus events from WindowTracker and maintains a
    rolling graph that can be analyzed for thrashing patterns.

    Usage:
        builder = FocusGraphBuilder()
        builder.add_event(app_name="Terminal", window_title="zsh", timestamp=now)
        builder.add_event(app_name="Code", window_title="auth.ts", timestamp=now+2)
        score = builder.compute_thrashing_score()
    """

    def __init__(
        self,
        window_seconds: float = _DEFAULT_WINDOW_SECONDS,
        max_events: int = 500,
    ) -> None:
        self._window_seconds = window_seconds
        self._events: list[_FocusEvent] = []
        self._max_events = max_events
        self._current_node_id: str | None = None
        self._current_enter_ts: float = 0.0

    @staticmethod
    def _make_node_id(app_name: str, window_title: str) -> str:
        """Create a stable node ID from app name and title."""
        # Use first 50 chars of title to group similar windows
        title_key = window_title[:50].strip().lower()
        title_hash = hashlib.md5(title_key.encode()).hexdigest()[:8]
        return f"{app_name}:{title_hash}"

    def add_event(
        self,
        app_name: str,
        window_title: str,
        timestamp: float | None = None,
    ) -> None:
        """
        Record a focus change event.

        Args:
            app_name: Name of the application that gained focus.
            window_title: Window/tab title.
            timestamp: Event timestamp. None = use time.monotonic().
        """
        if timestamp is None:
            timestamp = time.monotonic()

        node_id = self._make_node_id(app_name, window_title)

        # Skip if same node (no actual switch)
        if node_id == self._current_node_id:
            return

        event = _FocusEvent(
            node_id=node_id,
            app_name=app_name,
            window_title=window_title,
            timestamp=timestamp,
        )

        self._events.append(event)
        self._current_node_id = node_id
        self._current_enter_ts = timestamp

        # Trim old events
        if len(self._events) > self._max_events:
            self._events = self._events[-self._max_events:]

    def compute_thrashing_score(
        self,
        window_seconds: float | None = None,
        current_time: float | None = None,
    ) -> float:
        """
        Compute thrashing score for the analysis window.

        High score (>0.7) indicates rapid context switching between
        multiple distinct windows — a sign of cognitive thrashing.

        Scoring factors:
        - Number of unique nodes visited (normalized)
        - Switching velocity (switches per second)
        - Mean dwell time (shorter = more thrashing)
        - Revisit ratio (bouncing back to same nodes)

        Args:
            window_seconds: Override analysis window. None = use default.
            current_time: Override current time. None = use time.monotonic().

        Returns:
            Thrashing score in [0, 1].
        """
        if current_time is None:
            current_time = time.monotonic()
        window = window_seconds or self._window_seconds

        # Get events in window
        cutoff = current_time - window
        recent = [e for e in self._events if e.timestamp >= cutoff]

        if len(recent) < 2:
            return 0.0

        # Count unique nodes
        unique_nodes = set(e.node_id for e in recent)
        n_unique = len(unique_nodes)
        n_switches = len(recent) - 1

        if n_unique < _MIN_NODES_FOR_THRASHING:
            return 0.0

        # Factor 1: Node diversity (more unique nodes = more thrashing)
        # Normalize: 3 nodes → 0.3, 6+ nodes → 1.0
        diversity_score = min(1.0, (n_unique - 2) / 4.0)

        # Factor 2: Switching velocity
        time_span = recent[-1].timestamp - recent[0].timestamp
        if time_span < 1.0:
            velocity = float(n_switches)
        else:
            velocity = n_switches / time_span
        velocity_score = min(1.0, velocity / 1.0)  # 1 switch/sec = max

        # Factor 3: Mean dwell time (shorter = worse)
        dwells = []
        for i in range(len(recent) - 1):
            dwell = recent[i + 1].timestamp - recent[i].timestamp
            dwells.append(dwell)
        mean_dwell = sum(dwells) / len(dwells) if dwells else 60.0
        # 5s dwell → score 1.0, 30s → 0.0
        dwell_score = max(0.0, min(1.0, (30.0 - mean_dwell) / 25.0))

        # Factor 4: Revisit ratio (bouncing between same nodes)
        total_visits = len(recent)
        revisit_ratio = 1.0 - (n_unique / total_visits)
        revisit_score = min(1.0, revisit_ratio * 2.0)

        # Weighted combination
        score = (
            0.30 * diversity_score
            + 0.30 * velocity_score
            + 0.25 * dwell_score
            + 0.15 * revisit_score
        )

        return float(min(1.0, max(0.0, score)))

    def get_alignment_score(
        self,
        goal_keywords: list[str],
        window_seconds: float | None = None,
        current_time: float | None = None,
    ) -> float:
        """
        Compute how well current focus aligns with the session goal.

        Measures what fraction of dwell time is spent on windows
        whose titles contain goal-related keywords.

        Args:
            goal_keywords: Keywords from the session goal.
            window_seconds: Analysis window. None = use default.
            current_time: Override time. None = use time.monotonic().

        Returns:
            Alignment score in [0, 1]. 1.0 = perfectly on-task.
        """
        if not goal_keywords:
            return 1.0  # No goal set → assume aligned

        if current_time is None:
            current_time = time.monotonic()
        window = window_seconds or self._window_seconds

        cutoff = current_time - window
        recent = [e for e in self._events if e.timestamp >= cutoff]

        if len(recent) < 2:
            return 1.0

        # Compute dwell-weighted alignment
        keywords_lower = [k.lower() for k in goal_keywords]
        aligned_dwell = 0.0
        total_dwell = 0.0

        for i in range(len(recent) - 1):
            dwell = recent[i + 1].timestamp - recent[i].timestamp
            total_dwell += dwell

            title_lower = recent[i].window_title.lower()
            app_lower = recent[i].app_name.lower()
            combined = f"{app_lower} {title_lower}"

            if any(kw in combined for kw in keywords_lower):
                aligned_dwell += dwell

        if total_dwell < 1.0:
            return 1.0

        return float(aligned_dwell / total_dwell)

    def get_top_nodes(
        self,
        n: int = 5,
        window_seconds: float | None = None,
        current_time: float | None = None,
    ) -> list[dict]:
        """
        Get the most-visited nodes in the analysis window.

        Returns list of dicts with node_id, app_name, title, visit_count, total_dwell_s.
        """
        if current_time is None:
            current_time = time.monotonic()
        window = window_seconds or self._window_seconds
        cutoff = current_time - window
        recent = [e for e in self._events if e.timestamp >= cutoff]

        if len(recent) < 2:
            return []

        # Aggregate by node
        node_info: dict[str, dict] = {}
        for i, event in enumerate(recent):
            if event.node_id not in node_info:
                node_info[event.node_id] = {
                    "node_id": event.node_id,
                    "app_name": event.app_name,
                    "title": event.window_title[:80],
                    "visit_count": 0,
                    "total_dwell_s": 0.0,
                }
            node_info[event.node_id]["visit_count"] += 1
            if i < len(recent) - 1:
                dwell = recent[i + 1].timestamp - event.timestamp
                node_info[event.node_id]["total_dwell_s"] += dwell

        # Sort by visit count descending
        sorted_nodes = sorted(
            node_info.values(),
            key=lambda x: x["visit_count"],
            reverse=True,
        )
        return sorted_nodes[:n]

    def get_recent_transitions(
        self,
        n: int = 10,
        window_seconds: float | None = None,
        current_time: float | None = None,
    ) -> list[dict]:
        """Get recent transitions for LLM context."""
        if current_time is None:
            current_time = time.monotonic()
        window = window_seconds or self._window_seconds
        cutoff = current_time - window
        recent = [e for e in self._events if e.timestamp >= cutoff]

        transitions = []
        for i in range(max(0, len(recent) - n), len(recent) - 1):
            transitions.append({
                "from": f"{recent[i].app_name}: {recent[i].window_title[:60]}",
                "to": f"{recent[i+1].app_name}: {recent[i+1].window_title[:60]}",
                "dwell_seconds": round(recent[i+1].timestamp - recent[i].timestamp, 1),
            })
        return transitions

    def clear(self) -> None:
        """Clear all events."""
        self._events.clear()
        self._current_node_id = None
        self._current_enter_ts = 0.0
