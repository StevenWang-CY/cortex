"""
State Engine — Longitudinal Baseline Drift (Trend Model)

Tracks how physiological baselines drift across days and weeks.
Identifies:
- When the user tends to enter overload (time-of-day patterns)
- Which task types/repos trigger it
- Which websites correlate with recovery vs. derailment

Uses this to dynamically adjust the sensitivity of the stress integral.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# Default analysis window
_DEFAULT_WINDOW_DAYS = 30
_MIN_DAYS_FOR_TREND = 7


class LongitudinalTracker:
    """
    Background chronotype model tracking baseline drift across weeks.

    Stores daily baseline summaries and computes trends to dynamically
    adjust the stress integral sensitivity.

    Usage:
        tracker = LongitudinalTracker(store=redis_store)
        await tracker.record_daily_summary(hr=72, hrv=48, resp=14, ...)
        multiplier = await tracker.get_sensitivity_multiplier()
    """

    def __init__(
        self,
        store: Any = None,
        window_days: int = _DEFAULT_WINDOW_DAYS,
    ) -> None:
        self._store = store
        self._window_days = window_days
        self._sensitivity_multiplier: float = 1.0

        # In-memory accumulators for current day
        self._today = date.today()
        self._hr_samples: list[float] = []
        self._hrv_samples: list[float] = []
        self._resp_samples: list[float] = []
        self._hourly_overload: dict[int, list[bool]] = defaultdict(list)
        self._flow_seconds: float = 0.0
        self._hyper_seconds: float = 0.0
        self._intervention_count: int = 0
        self._intervention_accepted: int = 0

        # Per-topic tracking for subject-specific calibration
        self._topic_stress: dict[str, list[float]] = defaultdict(list)
        self._topic_flow: dict[str, float] = defaultdict(float)
        self._topic_hyper: dict[str, float] = defaultdict(float)
        self._current_topic: str | None = None

    def accumulate(
        self,
        hr: float | None = None,
        hrv: float | None = None,
        resp: float | None = None,
        state: str = "FLOW",
        dt_seconds: float = 0.5,
    ) -> None:
        """
        Accumulate a single sample into today's running totals.

        Called from the state loop (~2Hz).
        """
        current = date.today()
        if current != self._today:
            self._today = current
            self._hr_samples.clear()
            self._hrv_samples.clear()
            self._resp_samples.clear()
            self._hourly_overload.clear()
            self._flow_seconds = 0.0
            self._hyper_seconds = 0.0
            self._intervention_count = 0
            self._intervention_accepted = 0

        if hr is not None:
            self._hr_samples.append(hr)
        if hrv is not None:
            self._hrv_samples.append(hrv)
        if resp is not None:
            self._resp_samples.append(resp)

        hour = datetime.now().hour
        self._hourly_overload[hour].append(state == "HYPER")

        if state == "FLOW":
            self._flow_seconds += dt_seconds
        elif state == "HYPER":
            self._hyper_seconds += dt_seconds

        # Per-topic accumulation
        if self._current_topic:
            if state == "FLOW":
                self._topic_flow[self._current_topic] += dt_seconds
            elif state == "HYPER":
                self._topic_hyper[self._current_topic] += dt_seconds
            if hrv is not None:
                self._topic_stress[self._current_topic].append(hrv)

    def record_intervention(self, accepted: bool) -> None:
        """Record an intervention event."""
        self._intervention_count += 1
        if accepted:
            self._intervention_accepted += 1

    async def snapshot_daily(self) -> dict:
        """
        Create a daily baseline summary from accumulated samples.

        Called every hour by the longitudinal loop.
        Returns the summary dict for storage.
        """
        summary = {
            "date": self._today.isoformat(),
            "hr_baseline": float(np.mean(self._hr_samples)) if self._hr_samples else 72.0,
            "hrv_baseline": float(np.mean(self._hrv_samples)) if self._hrv_samples else 50.0,
            "resp_baseline": float(np.mean(self._resp_samples)) if self._resp_samples else 15.0,
            "total_flow_minutes": self._flow_seconds / 60.0,
            "total_hyper_minutes": self._hyper_seconds / 60.0,
            "peak_overload_hours": self._compute_peak_hours(),
            "interventions_count": self._intervention_count,
            "interventions_accepted": self._intervention_accepted,
        }

        # Persist to store
        if self._store is not None:
            key = f"daily_baseline:{self._today.isoformat()}"
            try:
                await self._store.set_json(key, summary, ttl_seconds=90 * 86400)
            except Exception:
                logger.exception("Failed to persist daily baseline")

        return summary

    def _compute_peak_hours(self) -> list[int]:
        """Find hours with highest overload rate."""
        if not self._hourly_overload:
            return []

        rates = {}
        for hour, samples in self._hourly_overload.items():
            if len(samples) >= 10:  # Need enough samples
                rates[hour] = sum(samples) / len(samples)

        if not rates:
            return []

        # Return hours with overload rate > 30%
        threshold = 0.3
        peak = [h for h, r in sorted(rates.items()) if r > threshold]
        return peak[:5]  # Top 5

    async def compute_trend(self) -> dict:
        """
        Compute trends from stored daily baselines.

        Returns trend information including sensitivity multiplier.
        """
        if self._store is None:
            return {"trend": "stable", "sensitivity_multiplier": 1.0}

        # Load recent daily baselines from store
        baselines = []
        for i in range(self._window_days):
            from datetime import timedelta
            d = date.today() - timedelta(days=i)
            key = f"daily_baseline:{d.isoformat()}"
            try:
                data = await self._store.get_json(key)
                if data:
                    baselines.append(data)
            except Exception:
                continue

        if len(baselines) < _MIN_DAYS_FOR_TREND:
            return {"trend": "stable", "sensitivity_multiplier": 1.0}

        # Sort by date
        baselines.sort(key=lambda x: x["date"])

        # Compute HRV trend via linear regression
        hrv_values = [b.get("hrv_baseline", 50.0) for b in baselines]
        x = np.arange(len(hrv_values), dtype=np.float64)
        y = np.array(hrv_values, dtype=np.float64)

        if len(x) < 2 or np.std(y) < 1e-6:
            trend = "stable"
            slope = 0.0
        else:
            slope = float(np.polyfit(x, y, 1)[0])
            if slope > 0.5:
                trend = "improving"
            elif slope < -0.5:
                trend = "declining"
            else:
                trend = "stable"

        # Adjust sensitivity based on trend
        # Declining HRV → increase sensitivity (lower threshold)
        # Improving HRV → decrease sensitivity (higher threshold)
        if trend == "declining":
            multiplier = max(0.5, 1.0 + slope * 0.1)  # slope is negative
        elif trend == "improving":
            multiplier = min(1.5, 1.0 + slope * 0.05)
        else:
            multiplier = 1.0

        self._sensitivity_multiplier = multiplier

        result = {
            "trend": trend,
            "sensitivity_multiplier": multiplier,
            "hrv_slope_per_day": slope,
            "days_analyzed": len(baselines),
            "mean_hrv": float(np.mean(hrv_values)),
        }

        logger.info(
            "Longitudinal trend: %s (slope=%.2f, multiplier=%.2f, %d days)",
            trend, slope, multiplier, len(baselines),
        )
        return result

    def set_topic(self, topic: str | None) -> None:
        """Set the current topic tag for per-subject tracking."""
        self._current_topic = topic

    def get_topic_difficulty(self, topic: str) -> float | None:
        """Get the relative difficulty of a topic (0-1 scale).

        Based on the ratio of HYPER time to total tracked time for this topic.
        Returns None if insufficient data (<60s tracked).
        """
        flow = self._topic_flow.get(topic, 0.0)
        hyper = self._topic_hyper.get(topic, 0.0)
        total = flow + hyper
        if total < 60.0:
            return None
        return hyper / total

    def get_topic_stress_modifier(self, topic: str) -> float:
        """Get stress integral sensitivity modifier for a topic.

        Hard topics (high HYPER ratio) get a higher threshold (more patience).
        Easy topics get a lower threshold (break sooner if struggling).

        Returns a multiplier in [0.7, 1.3].
        """
        difficulty = self.get_topic_difficulty(topic)
        if difficulty is None:
            return 1.0
        # Hard topic → higher multiplier → more patience (up to 1.3)
        # Easy topic → lower multiplier → less patience (down to 0.7)
        return 0.7 + difficulty * 0.6

    @property
    def sensitivity_multiplier(self) -> float:
        """Current sensitivity multiplier for stress integral."""
        return self._sensitivity_multiplier
