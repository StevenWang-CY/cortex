"""Session Report — generation from accumulated session data."""

from __future__ import annotations

import logging
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone

from cortex.services.session_report.models import (
    ActivitySummary,
    ComparisonStats,
    SessionReport,
    StateTransition,
)

logger = logging.getLogger(__name__)


class SessionReportGenerator:
    """Accumulates session events and generates a final report.

    Usage:
        gen = SessionReportGenerator()
        gen.start()
        gen.record_state("FLOW", timestamp)
        gen.record_state("HYPER", timestamp)
        gen.record_hr(72.0)
        gen.record_hrv(45.0)
        gen.record_break(recommended=True)
        gen.record_activity("Lecture 3", "educational", 300.0)
        report = gen.finish()
    """

    def __init__(self) -> None:
        self._session_id = str(uuid.uuid4())[:8]
        self._start_time: datetime | None = None
        self._current_state: str | None = None
        self._current_state_start: float = 0.0
        self._state_durations: dict[str, float] = defaultdict(float)
        self._state_transitions: list[StateTransition] = []
        self._flow_streaks: list[float] = []
        self._current_flow_start: float | None = None
        self._hr_samples: list[float] = []
        self._hrv_samples: list[float] = []
        self._peak_stress: float = 0.0
        self._breaks_taken: int = 0
        self._breaks_recommended: int = 0
        self._activities: list[ActivitySummary] = []
        self._distraction_domains: list[str] = []
        self._hourly_flow: dict[int, float] = defaultdict(float)
        self._hourly_total: dict[int, float] = defaultdict(float)

    def start(self) -> None:
        """Mark session start."""
        self._start_time = datetime.now(timezone.utc)

    def record_state(self, state: str, timestamp: float) -> None:
        """Record a state transition."""
        now = timestamp
        if self._current_state is not None:
            dt = now - self._current_state_start
            self._state_durations[self._current_state] += dt

            # Track hourly flow
            hour = datetime.fromtimestamp(self._current_state_start).hour
            self._hourly_total[hour] += dt
            if self._current_state == "FLOW":
                self._hourly_flow[hour] += dt

            self._state_transitions.append(StateTransition(
                from_state=self._current_state,
                to_state=state,
                timestamp=datetime.fromtimestamp(now, tz=timezone.utc),
            ))

        # Track flow streaks
        if state == "FLOW" and self._current_state != "FLOW":
            self._current_flow_start = now
        elif state != "FLOW" and self._current_state == "FLOW":
            if self._current_flow_start is not None:
                self._flow_streaks.append(now - self._current_flow_start)
            self._current_flow_start = None

        self._current_state = state
        self._current_state_start = now

    def record_hr(self, hr_bpm: float) -> None:
        """Record a heart rate sample."""
        if hr_bpm > 0:
            self._hr_samples.append(hr_bpm)

    def record_hrv(self, hrv_rmssd: float) -> None:
        """Record an HRV sample."""
        if hrv_rmssd > 0:
            self._hrv_samples.append(hrv_rmssd)

    def record_stress(self, stress_integral: float) -> None:
        """Record peak stress integral."""
        self._peak_stress = max(self._peak_stress, stress_integral)

    def record_break(self, *, recommended: bool = False) -> None:
        """Record a break event."""
        self._breaks_taken += 1
        if recommended:
            self._breaks_recommended += 1

    def record_activity(
        self, title: str, tab_type: str = "other", dwell_s: float = 0.0,
    ) -> None:
        """Record an activity."""
        self._activities.append(ActivitySummary(
            title=title, tab_type=tab_type, dwell_seconds=dwell_s,
        ))

    def record_distraction(self, domain: str) -> None:
        """Record a distraction domain."""
        self._distraction_domains.append(domain)

    def finish(
        self,
        comparison: ComparisonStats | None = None,
    ) -> SessionReport:
        """Generate the final session report."""
        end_time = datetime.now(timezone.utc)
        start = self._start_time or end_time

        duration = (end_time - start).total_seconds()

        # Finalize current state
        if self._current_state == "FLOW" and self._current_flow_start is not None:
            import time as _time
            self._flow_streaks.append(_time.time() - self._current_flow_start)

        flow_s = self._state_durations.get("FLOW", 0.0)
        hyper_s = self._state_durations.get("HYPER", 0.0)
        hypo_s = self._state_durations.get("HYPO", 0.0)
        recovery_s = self._state_durations.get("RECOVERY", 0.0)

        flow_pct = (flow_s / duration * 100.0) if duration > 0 else 0.0

        # Golden hour: hour with highest flow ratio
        golden_start: int | None = None
        golden_end: int | None = None
        best_ratio = 0.0
        for hour, total in self._hourly_total.items():
            if total > 0:
                ratio = self._hourly_flow.get(hour, 0.0) / total
                if ratio > best_ratio:
                    best_ratio = ratio
                    golden_start = hour
                    golden_end = (hour + 1) % 24

        # Top distraction domains
        domain_counts = Counter(self._distraction_domains)
        top_distractions = [d for d, _ in domain_counts.most_common(5)]

        # Top activities by dwell
        sorted_activities = sorted(
            self._activities, key=lambda a: a.dwell_seconds, reverse=True,
        )[:10]

        return SessionReport(
            session_id=self._session_id,
            start_time=start,
            end_time=end_time,
            duration_seconds=duration,
            time_in_flow_seconds=flow_s,
            time_in_hyper_seconds=hyper_s,
            time_in_hypo_seconds=hypo_s,
            time_in_recovery_seconds=recovery_s,
            flow_percentage=round(flow_pct, 1),
            longest_flow_streak_seconds=max(self._flow_streaks) if self._flow_streaks else 0.0,
            peak_stress_integral=self._peak_stress,
            breaks_taken=self._breaks_taken,
            breaks_recommended=self._breaks_recommended,
            state_transitions=self._state_transitions,
            top_activities=sorted_activities,
            top_distraction_domains=top_distractions,
            golden_hour_start=golden_start,
            golden_hour_end=golden_end,
            avg_hr_bpm=round(sum(self._hr_samples) / len(self._hr_samples), 1) if self._hr_samples else None,
            avg_hrv_rmssd=round(sum(self._hrv_samples) / len(self._hrv_samples), 1) if self._hrv_samples else None,
            comparison_to_7day=comparison,
        )
