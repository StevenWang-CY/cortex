"""Session Report — generation from accumulated session data."""

from __future__ import annotations

import logging
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any

from cortex.services.session_report.models import (
    ActivitySummary,
    BreakRecord,
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
        # P0 §3.7: per-break audit trail. Populated by the
        # BiologyBreakController on session end so the recap card can
        # show "+24 HRV recovery during break" without re-reading
        # storage.
        self._break_records: list[BreakRecord] = []
        # P1 Pipeline B: real intervention counters wired from the
        # runtime daemon. Longitudinal roll-ups previously aliased both
        # to "HYPER state transitions"; now they reflect actual plan
        # acceptance.
        self._interventions_triggered: int = 0
        self._interventions_accepted: int = 0
        # P0 §3.13: the most-recent user-provided session goal. Stamped
        # onto the SessionReport at ``finish()`` so longitudinal
        # aggregators can later detect a "task_overload_pattern" trend.
        self._goal_title: str | None = None
        # B22 (Phase 4.1): list of clock-anomaly events observed during
        # the session. Each entry is a ``{"timestamp": float, "kind":
        # "ntp_backjump", "dt_seconds": float, "state": str}`` dict.
        # Appended by ``record_state`` when ``raw_dt < 0`` triggers the
        # backjump clamp; surfaced on the report so the recap can show a
        # "clock anomaly" badge instead of silently producing an
        # implausible duration roll-up.
        self._clock_anomalies: list[dict[str, Any]] = []

    def set_goal_title(self, title: str | None) -> None:
        """P0 §3.13: stamp the user-provided session goal.

        ``None`` or empty string clears the goal. Re-setting overwrites
        the prior value — the latest goal at ``finish()`` time wins, which
        matches the UX: the goal input is editable mid-session.
        """
        if not title:
            self._goal_title = None
            return
        trimmed = title.strip()[:240]
        self._goal_title = trimmed or None

    @property
    def clock_anomalies(self) -> list[dict[str, Any]]:
        """B22 (Phase 4.1): observed clock-anomaly events.

        Each entry is a ``{"timestamp", "kind", "dt_seconds", "state"}``
        dict. The recap UI reads this to surface a "clock anomaly"
        badge when the report's durations are derived from a session
        that crossed an NTP backjump.
        """
        return list(self._clock_anomalies)

    def start(self) -> None:
        """Mark session start."""
        self._start_time = datetime.now(UTC)

    def record_state(self, state: str, timestamp: float) -> None:
        """Record a state transition."""
        now = timestamp
        if self._current_state is not None:
            # P0 Pipeline B: clamp negative dt to defend against NTP
            # backjumps (system clock leaping backwards while the daemon
            # is running). Without the clamp, the duration counter could
            # accumulate negative values and the percentage roll-up
            # would lie about how long the user spent in each state.
            raw_dt = now - self._current_state_start
            if raw_dt < 0:
                logger.warning(
                    "Negative state duration detected (dt=%.3fs, state=%s) — "
                    "clamping to 0. NTP backjump or non-monotonic input?",
                    raw_dt,
                    self._current_state,
                )
                # B22 (Phase 4.1): record the anomaly so the session
                # report can carry a structured event the recap UI can
                # render as a "clock anomaly" badge. Without this the
                # clamp silently swallowed the discrepancy and the user
                # had no signal that their durations were not authoritative.
                self._clock_anomalies.append({
                    "timestamp": now,
                    "kind": "ntp_backjump",
                    "dt_seconds": raw_dt,
                    "state": self._current_state,
                })
            dt = max(0.0, raw_dt)
            self._state_durations[self._current_state] += dt

            # Track hourly flow (use UTC consistently — F26).
            hour = datetime.fromtimestamp(self._current_state_start, tz=UTC).hour
            self._hourly_total[hour] += dt
            if self._current_state == "FLOW":
                self._hourly_flow[hour] += dt

            self._state_transitions.append(StateTransition(
                from_state=self._current_state,
                to_state=state,
                timestamp=datetime.fromtimestamp(now, tz=UTC),
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

    def record_break(
        self,
        *,
        recommended: bool = False,
        taken: bool = True,
        record: BreakRecord | None = None,
    ) -> None:
        """Record a break event.

        P0 §3.7 extension: ``recommended`` reflects whether the daemon
        nudged the user via :attr:`BREAK_RECOMMENDATION`; ``taken``
        reflects whether the user actually started the breathing
        session. When ``record`` is provided it is appended to the
        per-session audit trail (one entry per guided session).
        """
        if taken:
            self._breaks_taken += 1
        if recommended:
            self._breaks_recommended += 1
        if record is not None:
            self._break_records.append(record)

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

    def increment_interventions_triggered(self, count: int = 1) -> None:
        """Increment the per-session triggered-plan counter (P1 Pipeline B).

        Called by the runtime daemon every time the trigger policy approves
        a plan AND the executor delivered it to the user. Independent from
        :meth:`increment_interventions_accepted` so the gap between the
        two (which equals "dismissed / never-rated") can be read off the
        session report directly.
        """
        if count < 0:
            return
        self._interventions_triggered += int(count)

    def increment_interventions_accepted(self, count: int = 1) -> None:
        """Increment the per-session accepted-plan counter (P1 Pipeline B).

        Called by the runtime daemon when the user posts a Keep / thumbs-
        up outcome for a triggered plan. The caller is responsible for
        sequencing — this counter must never exceed ``interventions_triggered``
        for the same session, but the generator does not enforce that.
        """
        if count < 0:
            return
        self._interventions_accepted += int(count)

    def finish(
        self,
        comparison: ComparisonStats | None = None,
        end_timestamp: float | None = None,
    ) -> SessionReport:
        """Generate the final session report.

        Args:
            comparison: Optional 7-day comparison stats.
            end_timestamp: Optional epoch timestamp for the session end.
                If None, uses time.time(). Pass explicitly in tests with
                synthetic timestamps.
        """
        import time as _time

        end_ts = end_timestamp if end_timestamp is not None else _time.time()
        end_time = datetime.fromtimestamp(end_ts, tz=UTC)
        start = self._start_time or end_time

        duration = (end_time - start).total_seconds()

        # Finalize current state — close the last segment's duration
        if self._current_state is not None:
            dt = end_ts - self._current_state_start
            if dt > 0:
                self._state_durations[self._current_state] += dt
                hour = datetime.fromtimestamp(self._current_state_start, tz=UTC).hour
                self._hourly_total[hour] += dt
                if self._current_state == "FLOW":
                    self._hourly_flow[hour] += dt
        if self._current_state == "FLOW" and self._current_flow_start is not None:
            streak = end_ts - self._current_flow_start
            if streak > 0:
                self._flow_streaks.append(streak)

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
            interventions_triggered=self._interventions_triggered,
            interventions_accepted=self._interventions_accepted,
            break_records=list(self._break_records),
            state_transitions=self._state_transitions,
            top_activities=sorted_activities,
            top_distraction_domains=top_distractions,
            golden_hour_start=golden_start,
            golden_hour_end=golden_end,
            avg_hr_bpm=round(sum(self._hr_samples) / len(self._hr_samples), 1) if self._hr_samples else None,
            avg_hrv_rmssd=round(sum(self._hrv_samples) / len(self._hrv_samples), 1) if self._hrv_samples else None,
            comparison_to_7day=comparison,
            goal_title=self._goal_title,
        )
