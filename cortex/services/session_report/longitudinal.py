"""
Cortex Longitudinal Aggregator (P0 §3.2).

Walks ``storage/sessions/*.json`` and materialises ``DailyBaseline`` +
``ChronotypeModel`` rollups under ``storage/chronotype/``. The aggregator
runs nightly (the daemon's :class:`MidnightScheduler`) and on explicit
``REQUEST_TRENDS`` calls with ``refresh=True``.

Design decisions (from P0_IMPLEMENTATION_DESIGN.md §3.2):

* Aggregator runs offline. ``REQUEST_TRENDS`` reads pre-computed
  rollups; we do NOT recompute every render.
* Storage layout (new, created lazily):

  ::

      storage/
        chronotype/
          daily/YYYY-MM-DD.json   (DailyBaseline atomic_write_json)
          model.json              (ChronotypeModel atomic_write_json)

* Bucketing uses ``start_time.astimezone(local_tz).date()`` — DST and
  clock changes are handled by Python's native tz awareness. The
  ``local_tz`` discovery prefers a real IANA zone (``ZoneInfo``) by
  reading ``/etc/localtime`` so DST transitions in the hour walk are
  correct; only when that probe fails do we fall back to the fixed
  offset from ``datetime.now().astimezone()``.
* Per-day means are weighted by ``duration_seconds`` so a 2-hour
  session counts twice as much as a 1-hour session for the daily HR /
  HRV baseline.
* ``trend_direction`` is the sign of a linear regression slope over
  the last ``window_days // 4`` hrv_baseline values, normalised to a
  *relative* (~5 %) threshold so a high-HRV user is not flagged
  "improving" for a small absolute change (P0 §3.2 fix #15).
* ``interventions_count`` per day = sum of HYPER state-transition
  counts (same proxy AMIP uses elsewhere; documented in-line).
* ``interventions_accepted`` mirrors ``interventions_count`` until we
  have an explicit accept counter on SessionReport.

Public API:

* :class:`LongitudinalAggregator` — instance bound to the sessions
  directory and the chronotype directory.
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Literal

try:  # py3.9+
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover — every supported runtime has zoneinfo
    ZoneInfo = None  # type: ignore[assignment]
    ZoneInfoNotFoundError = Exception  # type: ignore[assignment,misc]

from cortex.libs.schemas.longitudinal import (
    ChronotypeModel,
    DailyBaseline,
    HourlyOverloadRate,
    TaskOverloadPattern,
)
from cortex.libs.schemas.session_history import TrendsResponse
from cortex.libs.utils.atomic_write import atomic_write_json

logger = logging.getLogger(__name__)

# How long the cached ``model.json`` is considered fresh before
# ``get_trends`` recomputes implicitly. Six hours mirrors the spec's
# "stale model.json (user offline for weeks)" guidance — we don't want
# the dashboard to show fully fresh numbers for a model that's days old.
_MODEL_FRESHNESS_HOURS: float = 6.0

# Window in days the chronotype aggregator considers when rebuilding the
# model. Daemon's nightly tick and the on-demand refresh both reuse this
# constant so the 90-day rolling horizon is documented in exactly one
# place (P0 §3.2 fix #18, #35).
_CHRONOTYPE_WINDOW_DAYS: int = 90

# Relative HRV-change threshold (5 % of the mean recent HRV) above which
# the linear regression slope is classified as "improving"/"declining".
# Absolute thresholds (e.g. "0.5 rmssd/day") falsely tag low-HRV users
# (P0 §3.2 fix #15).
_HRV_TREND_REL_THRESHOLD: float = 0.05

# Mean-HRV floor below which the slope is forced to "stable" (1 ms RMSSD
# is biologically implausible; serves as a sanity guard against divide-
# by-zero in the relative slope computation).
_HRV_TREND_MEAN_FLOOR: float = 1e-3

# Default window sizes for the trends rollup. ``quarter`` was dropped
# at the wire-schema level (P0 §3.2 fix #5); the dict is kept tolerant
# so a daemon receiving a stale "quarter" payload still returns rows
# rather than 500ing — the WS / REST dispatch layer is the strict gate.
_WINDOWS: dict[str, int] = {"week": 7, "month": 30, "quarter": 90}


# Process-wide cache for the discovered local timezone. Computed once so
# the ``readlink`` syscall doesn't run on every bucketing call.
_LOCAL_TZ_CACHE: tzinfo | None = None


def _discover_local_tz() -> tzinfo:
    """Discover the local IANA timezone, falling back to a fixed offset.

    Prefers ``zoneinfo.ZoneInfo`` keyed on the zone name pulled from
    ``/etc/localtime`` (macOS and most Linux distros symlink that path
    to ``…/zoneinfo/<Zone>/<Name>``). Real ``ZoneInfo`` instances know
    about DST transitions, so timedelta arithmetic over them follows
    real elapsed time, not wall clock — which is exactly what the
    hour-bucketing code needs.

    Falls back to ``datetime.now().astimezone().tzinfo`` (a
    ``timezone(timedelta(...))`` fixed offset) when the probe fails;
    that path is correct for "no DST in this region" and at worst
    drifts by one hour during a transition.
    """
    if ZoneInfo is not None:
        try:
            target = os.readlink("/etc/localtime")
            marker = "/zoneinfo/"
            if marker in target:
                zone_name = target.split(marker, 1)[1]
                return ZoneInfo(zone_name)
        except OSError:
            # /etc/localtime doesn't exist or isn't a symlink.
            pass
        except ZoneInfoNotFoundError:
            logger.debug("longitudinal: ZoneInfo lookup failed; using fixed offset")
    # Fallback: best effort fixed offset for "now".
    fallback = datetime.now().astimezone().tzinfo
    if fallback is None:
        # Defensive — datetime.now().astimezone() always returns a
        # tzinfo on supported platforms; UTC is the only sensible
        # last-resort default.
        return UTC
    return fallback


def _local_tz() -> tzinfo:
    """Local timezone as a ``tzinfo``; result is process-cached.

    Wrapped in a helper so tests can monkey-patch it (e.g. by setting
    ``longitudinal._LOCAL_TZ_CACHE`` directly).
    """
    global _LOCAL_TZ_CACHE
    if _LOCAL_TZ_CACHE is None:
        _LOCAL_TZ_CACHE = _discover_local_tz()
    return _LOCAL_TZ_CACHE


def _safe_local_datetime(value: Any) -> datetime | None:
    """Coerce a timestamp to a local-tz datetime, returning None on failure."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        try:
            dt = datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(_local_tz())


@dataclass(frozen=True)
class _SessionFacts:
    """Cheap projection of a session JSON used by the aggregator.

    ``has_hr_sample`` / ``has_hrv_sample`` are explicit "real data
    present?" booleans. The wire schema (``DailyBaseline``) carries
    only the resulting baseline floats, but we keep the boolean here
    so the day-roll-up code can distinguish "no observation" from
    "observation == schema default" (P0 §3.2 fix #16).
    """

    session_id: str
    start_time: datetime
    end_time: datetime
    local_date: date
    duration_seconds: float
    time_in_flow_seconds: float
    time_in_hyper_seconds: float
    avg_hr_bpm: float | None
    avg_hrv_rmssd: float | None
    has_hr_sample: bool
    has_hrv_sample: bool
    interventions: int
    state_transitions: list[tuple[datetime, str]]
    top_distraction_domains: list[str]


@dataclass(frozen=True)
class _DailyAggregate:
    """In-process aggregate that carries an extra ``has_hr_sample`` /
    ``has_hrv_sample`` flag the wire ``DailyBaseline`` doesn't expose.

    The flags are consumed by the slope-normalisation step in
    :meth:`LongitudinalAggregator.refresh_chronotype` so a day with
    zero HRV observations is filtered out instead of contaminating
    the regression with the schema default ``hrv_baseline=50`` (P0
    §3.2 fix #15 / #16).
    """

    baseline: DailyBaseline
    has_hr_sample: bool
    has_hrv_sample: bool


def _load_session_facts(path: Path) -> _SessionFacts | None:
    """Parse one session JSON file into a ``_SessionFacts`` projection.

    Returns None and logs at WARNING level on any malformed input — the
    aggregator continues without that session.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("longitudinal: cannot read %s: %s", path, exc)
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("longitudinal: malformed JSON %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        return None

    session_id = data.get("session_id")
    if not isinstance(session_id, str):
        return None
    start_time = _safe_local_datetime(data.get("start_time"))
    end_time = _safe_local_datetime(data.get("end_time"))
    if start_time is None or end_time is None:
        return None
    local_date = start_time.date()

    try:
        duration_seconds = float(data.get("duration_seconds") or 0.0)
    except (TypeError, ValueError):
        duration_seconds = 0.0
    try:
        time_in_flow = float(data.get("time_in_flow_seconds") or 0.0)
    except (TypeError, ValueError):
        time_in_flow = 0.0
    try:
        time_in_hyper = float(data.get("time_in_hyper_seconds") or 0.0)
    except (TypeError, ValueError):
        time_in_hyper = 0.0

    avg_hr = data.get("avg_hr_bpm")
    avg_hrv = data.get("avg_hrv_rmssd")
    try:
        avg_hr_val: float | None = float(avg_hr) if avg_hr is not None else None
    except (TypeError, ValueError):
        avg_hr_val = None
    try:
        avg_hrv_val: float | None = float(avg_hrv) if avg_hrv is not None else None
    except (TypeError, ValueError):
        avg_hrv_val = None

    # State transitions for hour bucketing + intervention proxy.
    transitions: list[tuple[datetime, str]] = []
    interventions = 0
    raw_transitions = data.get("state_transitions") or []
    if isinstance(raw_transitions, list):
        for t in raw_transitions:
            if not isinstance(t, dict):
                continue
            ts = _safe_local_datetime(t.get("timestamp"))
            to_state = t.get("to_state")
            if ts is None or not isinstance(to_state, str):
                continue
            transitions.append((ts, to_state))
            if to_state == "HYPER":
                interventions += 1

    domains_raw = data.get("top_distraction_domains") or []
    domains: list[str] = [d for d in domains_raw if isinstance(d, str)] if isinstance(domains_raw, list) else []

    return _SessionFacts(
        session_id=session_id,
        start_time=start_time,
        end_time=end_time,
        local_date=local_date,
        duration_seconds=duration_seconds,
        time_in_flow_seconds=time_in_flow,
        time_in_hyper_seconds=time_in_hyper,
        avg_hr_bpm=avg_hr_val,
        avg_hrv_rmssd=avg_hrv_val,
        has_hr_sample=avg_hr_val is not None,
        has_hrv_sample=avg_hrv_val is not None,
        interventions=interventions,
        state_transitions=transitions,
        top_distraction_domains=domains,
    )


def _next_hour_boundary(dt: datetime) -> datetime:
    """Return the wall-clock-next "top of the hour" in ``dt``'s tz.

    Adding ``timedelta(hours=1)`` to a tz-aware datetime follows real
    elapsed time, so during a DST transition the boundary lands on the
    correct local hour mark on both sides of the change.
    """
    local = dt.astimezone(_local_tz())
    top = local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    # Return in the original tz so callers' arithmetic stays consistent.
    return top.astimezone(dt.tzinfo) if dt.tzinfo is not None else top


def _bucket_hyper_seconds_per_hour(facts: _SessionFacts) -> dict[int, float]:
    """Compute per-hour seconds spent in HYPER for one session.

    Walks ``state_transitions`` chronologically; between consecutive
    transitions the user is in the ``to_state`` of the earlier
    transition. We accumulate per-hour HYPER time by splitting the
    interval across local-tz hour boundaries (DST-safe — fix #14).

    Returns an empty dict if the transitions list is empty, mirroring
    the spec's "If transitions list is empty, return []" requirement.
    """
    if not facts.state_transitions:
        return {}
    # Sort defensively (the on-disk order should already be sorted).
    sorted_transitions = sorted(facts.state_transitions, key=lambda t: t[0])

    per_hour: dict[int, float] = defaultdict(float)
    # End of last segment is the session end (we don't have a "post"
    # state-transition recording the final state at end_time).
    boundaries: list[tuple[datetime, str]] = list(sorted_transitions)
    boundaries.append((facts.end_time, "_END_"))

    for i in range(len(boundaries) - 1):
        seg_start, state = boundaries[i]
        seg_end, _ = boundaries[i + 1]
        if state != "HYPER" or seg_end <= seg_start:
            continue
        # Split across local-tz hour boundaries.
        cursor = seg_start
        while cursor < seg_end:
            hour_top = _next_hour_boundary(cursor)
            slice_end = min(hour_top, seg_end)
            local_hour = cursor.astimezone(_local_tz()).hour
            per_hour[local_hour] += (slice_end - cursor).total_seconds()
            cursor = slice_end
    return dict(per_hour)


class LongitudinalAggregator:
    """Aggregates ``storage/sessions/*.json`` into per-day baselines
    and a chronotype model.

    Constructor does NOT touch the disk; the chronotype dir is created
    lazily on the first write.

    Thread-safety: all methods are GIL-bound (synchronous I/O); safe
    to call from ``asyncio.to_thread`` offloads. The daemon does so.
    """

    def __init__(self, sessions_dir: Path, chronotype_dir: Path) -> None:
        self._sessions_dir = sessions_dir
        self._chronotype_dir = chronotype_dir
        self._daily_dir = chronotype_dir / "daily"
        self._model_path = chronotype_dir / "model.json"

    # ── Public API ────────────────────────────────────────────────────

    def aggregate_day(self, d: date) -> DailyBaseline:
        """Compute the ``DailyBaseline`` for one local-tz date.

        Reads every session whose ``start_time.astimezone(local).date()``
        equals ``d`` and computes duration-weighted means. Atomically
        persists the result to ``chronotype/daily/<d>.json``.
        """
        facts = [f for f in self._iter_session_facts() if f.local_date == d]
        agg = self._build_aggregate(d, facts)
        self._write_daily(agg.baseline)
        return agg.baseline

    def refresh_chronotype(
        self, window_days: int = _CHRONOTYPE_WINDOW_DAYS,
    ) -> ChronotypeModel:
        """Rebuild ``model.json`` from the last ``window_days`` of
        sessions.

        Reads every session whose ``local_date`` falls within
        ``[today - window_days + 1, today]``. Aggregates per-day
        baselines (also written), per-hour overload rates, and per-task
        patterns. Computes trend direction from the most recent
        ``window_days // 4`` hrv values (with HRV-sample filtering and
        relative-threshold normalisation — fix #15).
        """
        today = datetime.now(_local_tz()).date()
        window_start = today - timedelta(days=window_days - 1)
        facts_in_window: list[_SessionFacts] = [
            f
            for f in self._iter_session_facts()
            if window_start <= f.local_date <= today
        ]

        # Per-day rollup: persist DailyBaseline for each day with data.
        per_day: dict[date, list[_SessionFacts]] = defaultdict(list)
        for f in facts_in_window:
            per_day[f.local_date].append(f)
        aggregates: list[_DailyAggregate] = []
        for d in sorted(per_day.keys()):
            agg = self._build_aggregate(d, per_day[d])
            self._write_daily(agg.baseline)
            aggregates.append(agg)

        # Trend direction: linear regression slope on the most recent
        # ``window_days // 4`` hrv values. Smaller than 3 points → stable.
        # P0 §3.2 fix #15: only count days that actually carry an HRV
        # observation (not days where the baseline is just the schema
        # default), and normalise the slope by the mean so a low-HRV
        # user is not flagged "improving" for a tiny absolute change.
        trend_window = max(3, window_days // 4)
        recent_aggs = [a for a in aggregates if a.has_hrv_sample][-trend_window:]
        recent_hrv = [a.baseline.hrv_baseline for a in recent_aggs]
        trend_direction: Literal["improving", "stable", "declining"] = "stable"
        if len(recent_hrv) >= 3:
            mean_hrv = sum(recent_hrv) / len(recent_hrv)
            if mean_hrv > _HRV_TREND_MEAN_FLOOR:
                slope = _linear_slope(recent_hrv)
                slope_relative = slope / mean_hrv
                if slope_relative > _HRV_TREND_REL_THRESHOLD:
                    trend_direction = "improving"
                elif slope_relative < -_HRV_TREND_REL_THRESHOLD:
                    trend_direction = "declining"

        hourly_patterns = self._compute_hourly_patterns(facts_in_window)
        task_patterns = self._compute_task_patterns(facts_in_window)

        baselines = [a.baseline for a in aggregates]
        model = ChronotypeModel(
            baselines=baselines,
            trend_direction=trend_direction,
            sensitivity_multiplier=1.0,
            hourly_patterns=hourly_patterns,
            task_patterns=task_patterns,
            last_updated=datetime.now(UTC),
            window_days=window_days,
        )
        atomic_write_json(self._model_path, model.model_dump(mode="json"))
        logger.info(
            "longitudinal: wrote model.json window=%d trend=%s baselines=%d hourly=%d tasks=%d",
            window_days,
            trend_direction,
            len(baselines),
            len(hourly_patterns),
            len(task_patterns),
        )
        return model

    def get_trends(
        self,
        window: Literal["week", "month", "quarter"],
        *,
        refresh: bool = False,
    ) -> TrendsResponse:
        """Return the trends rollup for the requested window.

        If ``refresh=True`` OR ``model.json`` is missing OR older than
        :data:`_MODEL_FRESHNESS_HOURS`, recomputes and atomic-writes
        first. Otherwise serves the cached model + per-day rows.

        ``quarter`` is accepted (90 days) for backward compat with
        callers built against the pre-fix wire schema, but the public
        ``TrendsResponse.window`` is now constrained to ``week|month``
        — so quarter requests are downgraded to ``"month"`` before the
        envelope is constructed.
        """
        days = _WINDOWS.get(window, 7)
        model = self._load_or_refresh_model(
            refresh=refresh, window_days=_CHRONOTYPE_WINDOW_DAYS,
        )
        daily = self._load_daily_window(days)
        wire_window: Literal["week", "month"] = (
            "month" if window in ("month", "quarter") else "week"
        )
        return TrendsResponse(
            window=wire_window,
            daily=daily,
            chronotype=model,
            last_aggregated=model.last_updated,
        )

    def nightly_tick(self) -> None:
        """Idempotent nightly aggregation: refresh the chronotype rollup.

        The midnight scheduler calls this at 00:05 local time. It also
        runs on demand via the daemon when ``REQUEST_TRENDS`` is sent
        with ``refresh=True``. ``refresh_chronotype`` already walks
        every day in the window (including yesterday) so the redundant
        ``aggregate_day(yesterday)`` call was removed (P0 §3.2 fix #18).
        """
        logger.info("longitudinal: nightly_tick starting")
        try:
            self.refresh_chronotype(window_days=_CHRONOTYPE_WINDOW_DAYS)
        except Exception:
            logger.exception("longitudinal: refresh_chronotype failed")
        logger.info("longitudinal: nightly_tick done")

    def backfill_if_needed(self) -> None:
        """One-shot startup backfill when chronotype dir is empty.

        Runs once at daemon start: if ``model.json`` does not exist OR
        the daily/ dir is empty AND we have any sessions on disk,
        performs a full ``refresh_chronotype(90)``. Cheap (O(N sessions)).
        """
        try:
            model_exists = self._model_path.is_file()
            daily_empty = (
                not self._daily_dir.exists()
                or not any(self._daily_dir.iterdir())
            )
            sessions_present = (
                self._sessions_dir.exists()
                and any(
                    p
                    for p in self._sessions_dir.iterdir()
                    if p.is_file() and p.suffix == ".json" and p.name.startswith("session_")
                )
            )
        except OSError as exc:
            logger.warning("longitudinal: backfill probe failed: %s", exc)
            return

        if not sessions_present:
            return
        if model_exists and not daily_empty:
            return

        logger.info("longitudinal: backfill starting (sessions present, rollups missing)")
        try:
            self.refresh_chronotype(window_days=_CHRONOTYPE_WINDOW_DAYS)
            logger.info("longitudinal: backfill complete")
        except Exception:
            logger.exception("longitudinal: backfill failed")

    # ── Internal helpers ──────────────────────────────────────────────

    def _iter_session_facts(self) -> list[_SessionFacts]:
        """Walk ``sessions_dir`` and project each file to :class:`_SessionFacts`.

        Malformed files are logged + skipped (the aggregator must
        survive partial writes from a SIGKILL).
        """
        if not self._sessions_dir.exists() or not self._sessions_dir.is_dir():
            return []
        facts: list[_SessionFacts] = []
        try:
            children = list(self._sessions_dir.iterdir())
        except OSError as exc:
            logger.warning("longitudinal: cannot iterate %s: %s", self._sessions_dir, exc)
            return []
        for p in children:
            if not p.is_file() or p.suffix != ".json" or not p.name.startswith("session_"):
                continue
            parsed = _load_session_facts(p)
            if parsed is not None:
                facts.append(parsed)
        return facts

    def _build_aggregate(
        self, d: date, facts: list[_SessionFacts],
    ) -> _DailyAggregate:
        """Compute one ``_DailyAggregate`` from the day's session facts.

        Means are weighted by ``duration_seconds``. Missing avg_hr /
        avg_hrv fall back to the DailyBaseline schema defaults (72 /
        50) on the wire, but the returned ``_DailyAggregate`` carries
        the ``has_hr_sample`` / ``has_hrv_sample`` booleans so the
        trend-slope code can filter out "no observation" days (P0 §3.2
        fix #16).
        """
        if not facts:
            # No sessions on that day — return a default baseline so
            # the UI can render an "empty" bar without crashing.
            return _DailyAggregate(
                baseline=DailyBaseline(record_date=d),
                has_hr_sample=False,
                has_hrv_sample=False,
            )

        # Weighted HR/HRV. If every session lacks the metric, use the
        # schema defaults; otherwise weight by per-session duration.
        hr_weight = sum(f.duration_seconds for f in facts if f.has_hr_sample)
        hrv_weight = sum(f.duration_seconds for f in facts if f.has_hrv_sample)
        has_hr_sample = hr_weight > 0
        has_hrv_sample = hrv_weight > 0

        if has_hr_sample:
            hr_baseline = sum(
                (f.avg_hr_bpm or 0.0) * f.duration_seconds
                for f in facts
                if f.has_hr_sample
            ) / hr_weight
            hr_baseline = max(40.0, min(120.0, hr_baseline))
        else:
            # Wire-level schema default — `has_hr_sample=False` flags
            # this as "no observation, do not include in slope".
            hr_baseline = 72.0

        if has_hrv_sample:
            hrv_baseline = sum(
                (f.avg_hrv_rmssd or 0.0) * f.duration_seconds
                for f in facts
                if f.has_hrv_sample
            ) / hrv_weight
            hrv_baseline = max(10.0, min(200.0, hrv_baseline))
        else:
            # Wire-level schema default — see HR comment above.
            hrv_baseline = 50.0

        total_flow_minutes = sum(f.time_in_flow_seconds for f in facts) / 60.0
        total_hyper_minutes = sum(f.time_in_hyper_seconds for f in facts) / 60.0
        interventions_count = sum(f.interventions for f in facts)
        # interventions_accepted: best-effort proxy until we persist
        # an explicit accept counter on SessionReport (audit Debt-1
        # follow-up).
        interventions_accepted = interventions_count

        # peak_overload_hours: bucket HYPER seconds per hour across the
        # day, keep the hours whose total is in the top 25 % (or at
        # least the single highest hour if data is sparse).
        hyper_per_hour: dict[int, float] = defaultdict(float)
        for f in facts:
            for hour, secs in _bucket_hyper_seconds_per_hour(f).items():
                hyper_per_hour[hour] += secs
        peak_hours: list[int] = []
        if hyper_per_hour:
            ranked = sorted(
                hyper_per_hour.items(), key=lambda kv: kv[1], reverse=True
            )
            top_value = ranked[0][1]
            if top_value > 0:
                # Hours within 25 % of the peak are part of the overload
                # window. Floor at the single peak hour to ensure at
                # least one entry.
                threshold = top_value * 0.75
                peak_hours = sorted(
                    [h for h, v in ranked if v >= threshold]
                )

        baseline = DailyBaseline(
            record_date=d,
            hr_baseline=hr_baseline,
            hrv_baseline=hrv_baseline,
            # resp_baseline is not currently persisted on SessionReport;
            # use the schema default (15) until we wire it through.
            stress_integral_total=0.0,
            stress_integral_threshold=500.0,
            peak_overload_hours=peak_hours,
            total_flow_minutes=total_flow_minutes,
            total_hyper_minutes=total_hyper_minutes,
            session_count=len(facts),
            interventions_count=interventions_count,
            interventions_accepted=interventions_accepted,
        )
        return _DailyAggregate(
            baseline=baseline,
            has_hr_sample=has_hr_sample,
            has_hrv_sample=has_hrv_sample,
        )

    def _compute_hourly_patterns(
        self, facts: list[_SessionFacts]
    ) -> list[HourlyOverloadRate]:
        """Roll up per-hour overload rates across the full window.

        ``overload_rate`` for hour ``h`` = total HYPER seconds in ``h``
        / total observed seconds in ``h`` across all sessions touching
        that hour. ``sample_count`` is the number of sessions touching
        the hour.

        DST-safe (fix #14): hour boundaries are computed in the local
        timezone via ``_next_hour_boundary`` and indexed by the local
        hour at ``cursor``. ``+ timedelta(hours=1)`` on a tz-aware
        datetime follows real elapsed time, so segments crossing a
        spring-forward / fall-back boundary land in the correct local
        hour buckets.
        """
        hyper_per_hour: dict[int, float] = defaultdict(float)
        observed_per_hour: dict[int, float] = defaultdict(float)
        sessions_per_hour: dict[int, int] = defaultdict(int)

        for f in facts:
            # Observed per hour = wall-clock time the session spent in
            # each hour, computed by walking from start_time to end_time.
            if f.end_time <= f.start_time:
                continue
            touched: set[int] = set()
            cursor = f.start_time
            while cursor < f.end_time:
                hour_top = _next_hour_boundary(cursor)
                slice_end = min(hour_top, f.end_time)
                local_hour = cursor.astimezone(_local_tz()).hour
                observed_per_hour[local_hour] += (slice_end - cursor).total_seconds()
                touched.add(local_hour)
                cursor = slice_end
            for h in touched:
                sessions_per_hour[h] += 1
            for hour, secs in _bucket_hyper_seconds_per_hour(f).items():
                hyper_per_hour[hour] += secs

        out: list[HourlyOverloadRate] = []
        for hour in sorted(observed_per_hour.keys()):
            obs = observed_per_hour[hour]
            if obs <= 0:
                continue
            rate = hyper_per_hour.get(hour, 0.0) / obs
            rate = max(0.0, min(1.0, rate))
            out.append(
                HourlyOverloadRate(
                    hour=hour,
                    overload_rate=rate,
                    sample_count=sessions_per_hour.get(hour, 0),
                )
            )
        return out

    def _compute_task_patterns(
        self, facts: list[_SessionFacts]
    ) -> list[TaskOverloadPattern]:
        """Top 10 hostnames by frequency in ``top_distraction_domains``
        across the window.

        ``overload_rate`` = fraction of sessions where the domain
        appeared in ``top_distraction_domains``. ``correlation`` is
        ``"trigger"`` when ``overload_rate > 0.5`` else ``"neutral"``.
        """
        if not facts:
            return []
        appearances: dict[str, int] = defaultdict(int)
        for f in facts:
            seen_in_session: set[str] = set()
            for domain in f.top_distraction_domains:
                if not domain:
                    continue
                # Hostname only — defense in depth, the writer already
                # extracts hostnames but a future regression should not
                # leak full URLs.
                hostname = domain.strip().lower()
                if "/" in hostname:
                    hostname = hostname.split("/", 1)[0]
                if not hostname or hostname in seen_in_session:
                    continue
                appearances[hostname] += 1
                seen_in_session.add(hostname)
        total_sessions = max(1, len(facts))
        ranked = sorted(
            appearances.items(), key=lambda kv: kv[1], reverse=True
        )[:10]
        out: list[TaskOverloadPattern] = []
        for hostname, count in ranked:
            rate = max(0.0, min(1.0, count / total_sessions))
            correlation: Literal["trigger", "recovery", "neutral"] = (
                "trigger" if rate > 0.5 else "neutral"
            )
            out.append(
                TaskOverloadPattern(
                    pattern_key=hostname,
                    overload_rate=rate,
                    avg_stress_integral=0.0,
                    correlation=correlation,
                )
            )
        return out

    def _write_daily(self, baseline: DailyBaseline) -> None:
        """Atomically write one ``DailyBaseline`` JSON to chronotype/daily/."""
        self._daily_dir.mkdir(parents=True, exist_ok=True)
        target = self._daily_dir / f"{baseline.record_date.isoformat()}.json"
        atomic_write_json(target, baseline.model_dump(mode="json"))

    def _load_or_refresh_model(
        self,
        *,
        refresh: bool,
        window_days: int,
    ) -> ChronotypeModel:
        """Return the cached ChronotypeModel, recomputing if stale or absent."""
        if refresh:
            return self.refresh_chronotype(window_days=window_days)
        if not self._model_path.is_file():
            return self.refresh_chronotype(window_days=window_days)
        # Freshness check.
        try:
            raw = self._model_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            logger.warning("longitudinal: model.json unreadable; refreshing")
            return self.refresh_chronotype(window_days=window_days)
        try:
            model = ChronotypeModel.model_validate(data)
        except Exception:
            logger.warning(
                "longitudinal: model.json failed validation; refreshing",
                exc_info=True,
            )
            return self.refresh_chronotype(window_days=window_days)
        if model.last_updated is not None:
            now = datetime.now(UTC)
            last = model.last_updated
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            age_hours = (now - last).total_seconds() / 3600.0
            # P0 §3.2 fix #17: a negative age means ``model.json`` was
            # written in the future relative to ``now`` — typically an
            # NTP rewind on a laptop. Treat it as stale and recompute
            # rather than trusting a model from "later than now".
            if age_hours < 0 or age_hours > _MODEL_FRESHNESS_HOURS:
                logger.info(
                    "longitudinal: model.json stale (age=%.1fh); refreshing",
                    age_hours,
                )
                return self.refresh_chronotype(window_days=window_days)
        return model

    def _load_daily_window(self, days: int) -> list[DailyBaseline]:
        """Load the last ``days`` ``DailyBaseline`` JSONs in ascending date order."""
        if not self._daily_dir.exists():
            return []
        today = datetime.now(_local_tz()).date()
        cutoff = today - timedelta(days=days - 1)
        out: list[DailyBaseline] = []
        try:
            children = list(self._daily_dir.iterdir())
        except OSError as exc:
            logger.warning("longitudinal: cannot iterate %s: %s", self._daily_dir, exc)
            return []
        for path in children:
            if not path.is_file() or path.suffix != ".json":
                continue
            stem = path.stem  # YYYY-MM-DD
            try:
                d = date.fromisoformat(stem)
            except ValueError:
                continue
            if d < cutoff or d > today:
                continue
            try:
                raw = path.read_text(encoding="utf-8")
                data = json.loads(raw)
                baseline = DailyBaseline.model_validate(data)
            except (OSError, json.JSONDecodeError):
                logger.warning("longitudinal: skip malformed daily file %s", path)
                continue
            except Exception:
                logger.warning(
                    "longitudinal: skip invalid daily baseline %s",
                    path,
                    exc_info=True,
                )
                continue
            out.append(baseline)
        out.sort(key=lambda b: b.record_date)
        return out


def _linear_slope(values: list[float]) -> float:
    """Ordinary-least-squares slope of the values against their index.

    Returns 0.0 if the series has fewer than 2 points or is constant.
    Used by ``trend_direction``; we only care about the sign and a
    coarse magnitude threshold so the math doesn't need numpy.
    """
    n = len(values)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(values) / n
    num = sum((xs[i] - mean_x) * (values[i] - mean_y) for i in range(n))
    den = sum((xs[i] - mean_x) ** 2 for i in range(n))
    if den == 0:
        return 0.0
    return num / den


__all__ = ["LongitudinalAggregator"]
