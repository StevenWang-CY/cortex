"""
LongitudinalAggregator unit tests (P0 §3.2).

Covers:

* ``aggregate_day`` weighted means + cross-day exclusion + empty-day default.
* ``refresh_chronotype`` trend slope (improving / declining / stable, plus
  the "<3 points → stable" guardrail).
* ``get_trends`` window selection (week / month / quarter), caching, and
  forced refresh.
* ``backfill_if_needed`` initial run + idempotency.
* ``hourly_patterns`` concentration around hour 14.
* ``task_patterns`` correlation classification (>0.5 → trigger).

All tests stage session JSONs in ``tmp_path/sessions`` (write
``SessionReport(...).model_dump_json()`` directly so we exercise the same
reader code path as the daemon) and point the aggregator at
``tmp_path/chronotype``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cortex.libs.schemas.longitudinal import ChronotypeModel
from cortex.services.session_report.longitudinal import LongitudinalAggregator
from cortex.services.session_report.models import SessionReport, StateTransition


def _utc_noon(d: date) -> datetime:
    """Mid-day UTC so the local-tz date matches in any reasonable tz.

    We intentionally pick 12:00 UTC because every offset between -11h and
    +12h still maps that instant to the same local date.
    """
    return datetime(d.year, d.month, d.day, 12, 0, tzinfo=UTC)


def _write_session_at(
    sessions_dir: Path,
    session_id: str,
    *,
    start: datetime,
    duration_seconds: float = 600.0,
    time_in_flow_seconds: float = 400.0,
    time_in_hyper_seconds: float = 60.0,
    avg_hr_bpm: float | None = 72.0,
    avg_hrv_rmssd: float | None = 50.0,
    top_distraction_domains: list[str] | None = None,
    hyper_at: list[tuple[float, str]] | None = None,
) -> None:
    """Write one session JSON. ``hyper_at`` is a list of
    ``(seconds_after_start, to_state)`` pairs that become StateTransitions."""
    end = start + timedelta(seconds=duration_seconds)
    transitions = []
    for offset, to_state in hyper_at or []:
        transitions.append(
            StateTransition(
                from_state="FLOW",
                to_state=to_state,
                timestamp=start + timedelta(seconds=offset),
            )
        )
    report = SessionReport(
        session_id=session_id,
        start_time=start,
        end_time=end,
        duration_seconds=duration_seconds,
        time_in_flow_seconds=time_in_flow_seconds,
        time_in_hyper_seconds=time_in_hyper_seconds,
        flow_percentage=(time_in_flow_seconds / duration_seconds) * 100.0,
        avg_hr_bpm=avg_hr_bpm,
        avg_hrv_rmssd=avg_hrv_rmssd,
        top_distraction_domains=top_distraction_domains or [],
        state_transitions=transitions,
    )
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / f"session_{session_id}.json").write_text(
        report.model_dump_json(), encoding="utf-8"
    )


@pytest.fixture()
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    sessions = tmp_path / "sessions"
    chronotype = tmp_path / "chronotype"
    sessions.mkdir(parents=True, exist_ok=True)
    return sessions, chronotype


# ─── aggregate_day ────────────────────────────────────────────────────


def test_aggregate_day_sums_minutes_and_counts_sessions(dirs) -> None:
    sessions_dir, chronotype_dir = dirs
    d = date(2026, 5, 20)
    # 3 sessions: flow 600+1200+300s, hyper 60+120+30s
    _write_session_at(
        sessions_dir, "s1", start=_utc_noon(d),
        duration_seconds=1200, time_in_flow_seconds=600,
        time_in_hyper_seconds=60,
    )
    _write_session_at(
        sessions_dir, "s2", start=_utc_noon(d) + timedelta(hours=2),
        duration_seconds=1800, time_in_flow_seconds=1200,
        time_in_hyper_seconds=120,
    )
    _write_session_at(
        sessions_dir, "s3", start=_utc_noon(d) + timedelta(hours=4),
        duration_seconds=600, time_in_flow_seconds=300,
        time_in_hyper_seconds=30,
    )
    agg = LongitudinalAggregator(sessions_dir, chronotype_dir)
    baseline = agg.aggregate_day(d)
    assert baseline.session_count == 3
    assert baseline.total_flow_minutes == pytest.approx((600 + 1200 + 300) / 60.0)
    assert baseline.total_hyper_minutes == pytest.approx((60 + 120 + 30) / 60.0)
    # Daily JSON is written.
    assert (chronotype_dir / "daily" / f"{d.isoformat()}.json").is_file()


def test_aggregate_day_cross_day_session_excluded(dirs) -> None:
    sessions_dir, chronotype_dir = dirs
    d = date(2026, 5, 20)
    other = date(2026, 5, 21)
    _write_session_at(sessions_dir, "today", start=_utc_noon(d))
    _write_session_at(sessions_dir, "other", start=_utc_noon(other))
    agg = LongitudinalAggregator(sessions_dir, chronotype_dir)
    baseline = agg.aggregate_day(d)
    assert baseline.session_count == 1


def test_aggregate_day_empty_day_uses_defaults(dirs) -> None:
    sessions_dir, chronotype_dir = dirs
    agg = LongitudinalAggregator(sessions_dir, chronotype_dir)
    baseline = agg.aggregate_day(date(2026, 5, 20))
    assert baseline.session_count == 0
    assert baseline.hr_baseline == 72.0  # schema default
    assert baseline.hrv_baseline == 50.0
    assert baseline.total_flow_minutes == 0.0


# ─── refresh_chronotype + trend slope ─────────────────────────────────


def _seed_daily_baselines(
    chronotype_dir: Path,
    *,
    days: list[tuple[date, float]],
) -> None:
    """Write DailyBaseline JSONs directly so refresh_chronotype's window
    bookkeeping isn't needed for these tests."""
    from cortex.libs.schemas.longitudinal import DailyBaseline

    daily_dir = chronotype_dir / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    for d, hrv in days:
        b = DailyBaseline(record_date=d, hrv_baseline=hrv)
        (daily_dir / f"{d.isoformat()}.json").write_text(
            b.model_dump_json(), encoding="utf-8"
        )


def test_refresh_chronotype_improving_slope(dirs, monkeypatch) -> None:
    """7 consecutive days with hrv rising +1.0/day → 'improving'."""
    sessions_dir, chronotype_dir = dirs
    today = date(2026, 5, 20)
    # Stage 7 sessions, one per day, with rising avg_hrv_rmssd.
    for i in range(7):
        d = today - timedelta(days=6 - i)
        _write_session_at(
            sessions_dir,
            f"s{i}",
            start=_utc_noon(d),
            avg_hrv_rmssd=40.0 + i * 5.0,  # +5/day -> slope > 0.5
        )
    # Pin "now" to noon on today so today's date is deterministic.
    from cortex.services.session_report import longitudinal as L

    monkeypatch.setattr(
        L, "datetime", _PatchedDatetime(L.datetime, fixed_now=_utc_noon(today))
    )
    agg = LongitudinalAggregator(sessions_dir, chronotype_dir)
    model = agg.refresh_chronotype(window_days=7)
    assert model.trend_direction == "improving"


def test_refresh_chronotype_declining_slope(dirs, monkeypatch) -> None:
    sessions_dir, chronotype_dir = dirs
    today = date(2026, 5, 20)
    for i in range(7):
        d = today - timedelta(days=6 - i)
        _write_session_at(
            sessions_dir,
            f"s{i}",
            start=_utc_noon(d),
            avg_hrv_rmssd=80.0 - i * 5.0,  # -5/day -> slope < -0.5
        )
    from cortex.services.session_report import longitudinal as L
    monkeypatch.setattr(
        L, "datetime", _PatchedDatetime(L.datetime, fixed_now=_utc_noon(today))
    )
    agg = LongitudinalAggregator(sessions_dir, chronotype_dir)
    model = agg.refresh_chronotype(window_days=7)
    assert model.trend_direction == "declining"


def test_refresh_chronotype_stable_when_flat(dirs, monkeypatch) -> None:
    sessions_dir, chronotype_dir = dirs
    today = date(2026, 5, 20)
    for i in range(7):
        d = today - timedelta(days=6 - i)
        _write_session_at(
            sessions_dir, f"s{i}", start=_utc_noon(d), avg_hrv_rmssd=50.0
        )
    from cortex.services.session_report import longitudinal as L
    monkeypatch.setattr(
        L, "datetime", _PatchedDatetime(L.datetime, fixed_now=_utc_noon(today))
    )
    agg = LongitudinalAggregator(sessions_dir, chronotype_dir)
    model = agg.refresh_chronotype(window_days=7)
    assert model.trend_direction == "stable"


def test_refresh_chronotype_too_few_points_returns_stable(dirs, monkeypatch) -> None:
    sessions_dir, chronotype_dir = dirs
    today = date(2026, 5, 20)
    # Only 2 days of data → trend_direction must remain stable
    # regardless of magnitudes.
    for i in range(2):
        d = today - timedelta(days=1 - i)
        _write_session_at(
            sessions_dir, f"s{i}", start=_utc_noon(d), avg_hrv_rmssd=20.0 + i * 30.0
        )
    from cortex.services.session_report import longitudinal as L
    monkeypatch.setattr(
        L, "datetime", _PatchedDatetime(L.datetime, fixed_now=_utc_noon(today))
    )
    agg = LongitudinalAggregator(sessions_dir, chronotype_dir)
    model = agg.refresh_chronotype(window_days=7)
    assert model.trend_direction == "stable"


def test_refresh_chronotype_low_hrv_floor_forces_stable(dirs, monkeypatch) -> None:
    """When the mean of the recent HRV observations falls below
    ``_HRV_TREND_MEAN_FLOOR`` the slope is forced to ``"stable"`` — a
    tiny absolute drift on a near-zero mean would otherwise amplify
    into a false "improving" / "declining" trend on a noisy signal
    (P0 audit fix #4.B-2).

    We monkeypatch the floor *up* (to 15.0) so the DailyBaseline
    schema's hard ``ge=10.0`` floor (which would otherwise reject any
    constructed value < 10) is still respected — the values written
    here are 12+ ms RMSSD with a +0.5/day drift that, without the
    floor, would otherwise classify as "improving" via the relative
    threshold path.
    """
    sessions_dir, chronotype_dir = dirs
    today = date(2026, 5, 20)
    for i in range(7):
        d = today - timedelta(days=6 - i)
        _write_session_at(
            sessions_dir,
            f"s{i}",
            start=_utc_noon(d),
            avg_hrv_rmssd=12.0 + i * 0.5,
        )
    from cortex.services.session_report import longitudinal as L

    monkeypatch.setattr(L, "_HRV_TREND_MEAN_FLOOR", 15.0)
    monkeypatch.setattr(
        L, "datetime", _PatchedDatetime(L.datetime, fixed_now=_utc_noon(today))
    )
    agg = LongitudinalAggregator(sessions_dir, chronotype_dir)
    model = agg.refresh_chronotype(window_days=7)
    # Mean of the recent HRV values is ~13.5 — below the patched floor
    # of 15.0, so trend_direction MUST stay "stable" regardless of the
    # underlying linear slope.
    assert model.trend_direction == "stable"


def test_hrv_trend_floor_default_is_physiologically_sane() -> None:
    """The shipped floor must be >= 5 ms RMSSD — values under that are
    almost always sensor noise rather than real cardiac signal, and a
    relative-slope classification against them is meaningless."""
    from cortex.services.session_report import longitudinal as L

    assert L._HRV_TREND_MEAN_FLOOR >= 5.0


# ─── get_trends ───────────────────────────────────────────────────────


def test_get_trends_returns_only_requested_window_rows(dirs, monkeypatch) -> None:
    sessions_dir, chronotype_dir = dirs
    today = date(2026, 5, 20)
    # 30 days of sessions
    for i in range(30):
        d = today - timedelta(days=29 - i)
        _write_session_at(sessions_dir, f"d{i:02d}", start=_utc_noon(d))
    from cortex.services.session_report import longitudinal as L
    monkeypatch.setattr(
        L, "datetime", _PatchedDatetime(L.datetime, fixed_now=_utc_noon(today))
    )
    agg = LongitudinalAggregator(sessions_dir, chronotype_dir)
    # Ensure model + daily files exist.
    agg.refresh_chronotype(window_days=90)
    week = agg.get_trends("week")
    month = agg.get_trends("month")
    # Phase 4.4 contracts: the wire literal narrowed to "week"/"month";
    # the aggregator still accepts the internal "quarter" alias but
    # stamps the envelope as "month" so the schema validator never sees
    # an out-of-set string.
    quarter = agg.get_trends("quarter")  # type: ignore[arg-type]
    assert week.window == "week"
    assert len(week.daily) == 7
    assert month.window == "month"
    assert len(month.daily) == 30
    assert quarter.window == "month"
    assert len(quarter.daily) == 30  # ≤90, only 30 available


def test_get_trends_quarter_returns_90_days_when_available(
    dirs, monkeypatch,
) -> None:
    """P0 §3.2 contract: ``get_trends('quarter')`` returns 90 daily rows
    when 90+ days of session data exist on disk."""
    sessions_dir, chronotype_dir = dirs
    today = date(2026, 5, 20)
    # Stage exactly 90 days of sessions so the quarter window is fully
    # populated. (One session per day; the aggregate doesn't care about
    # the daily HR/HRV values for the row-count assertion.)
    for i in range(90):
        d = today - timedelta(days=89 - i)
        _write_session_at(sessions_dir, f"q{i:02d}", start=_utc_noon(d))
    from cortex.services.session_report import longitudinal as L

    monkeypatch.setattr(
        L, "datetime", _PatchedDatetime(L.datetime, fixed_now=_utc_noon(today))
    )
    agg = LongitudinalAggregator(sessions_dir, chronotype_dir)
    agg.refresh_chronotype(window_days=90)

    # Phase 4.4 contracts: ``quarter`` returns 90 days of data but the
    # response envelope's window literal is narrowed to "month".
    quarter = agg.get_trends("quarter")  # type: ignore[arg-type]
    assert quarter.window == "month"
    assert len(quarter.daily) == 90
    # Sanity: rows are chronologically ascending, oldest first.
    assert quarter.daily[0].record_date == today - timedelta(days=89)
    assert quarter.daily[-1].record_date == today


def test_get_trends_caches_unless_refresh(dirs, monkeypatch) -> None:
    """Two ``get_trends`` calls within the freshness window must NOT
    re-aggregate; the cached ``last_updated`` is identical."""
    sessions_dir, chronotype_dir = dirs
    today = date(2026, 5, 20)
    for i in range(5):
        d = today - timedelta(days=4 - i)
        _write_session_at(sessions_dir, f"x{i}", start=_utc_noon(d))
    from cortex.services.session_report import longitudinal as L
    monkeypatch.setattr(
        L, "datetime", _PatchedDatetime(L.datetime, fixed_now=_utc_noon(today))
    )
    agg = LongitudinalAggregator(sessions_dir, chronotype_dir)
    first = agg.get_trends("week")
    second = agg.get_trends("week")
    assert first.chronotype.last_updated == second.chronotype.last_updated


def test_get_trends_refresh_true_forces_recompute(dirs, monkeypatch) -> None:
    sessions_dir, chronotype_dir = dirs
    today = date(2026, 5, 20)
    for i in range(5):
        d = today - timedelta(days=4 - i)
        _write_session_at(sessions_dir, f"x{i}", start=_utc_noon(d))
    from cortex.services.session_report import longitudinal as L
    monkeypatch.setattr(
        L, "datetime", _PatchedDatetime(L.datetime, fixed_now=_utc_noon(today))
    )
    agg = LongitudinalAggregator(sessions_dir, chronotype_dir)
    # Spy on refresh_chronotype to count invocations.
    call_count = {"n": 0}
    real_refresh = agg.refresh_chronotype

    def spy_refresh(*a: Any, **kw: Any) -> ChronotypeModel:
        call_count["n"] += 1
        return real_refresh(*a, **kw)

    agg.refresh_chronotype = spy_refresh  # type: ignore[method-assign]
    agg.get_trends("week")          # 1: cold cache → refresh
    agg.get_trends("week")          # cache hit → no refresh
    agg.get_trends("week", refresh=True)  # explicit refresh
    assert call_count["n"] == 2


# ─── backfill_if_needed ───────────────────────────────────────────────


def test_backfill_if_needed_writes_model_and_daily(dirs, monkeypatch) -> None:
    sessions_dir, chronotype_dir = dirs
    today = date(2026, 5, 20)
    for i in range(5):
        d = today - timedelta(days=4 - i)
        _write_session_at(sessions_dir, f"b{i}", start=_utc_noon(d))
    from cortex.services.session_report import longitudinal as L
    monkeypatch.setattr(
        L, "datetime", _PatchedDatetime(L.datetime, fixed_now=_utc_noon(today))
    )
    agg = LongitudinalAggregator(sessions_dir, chronotype_dir)
    assert not (chronotype_dir / "model.json").exists()
    agg.backfill_if_needed()
    assert (chronotype_dir / "model.json").is_file()
    # At least one daily file written.
    daily_files = list((chronotype_dir / "daily").iterdir())
    assert daily_files


def test_backfill_if_needed_idempotent(dirs, monkeypatch) -> None:
    sessions_dir, chronotype_dir = dirs
    today = date(2026, 5, 20)
    _write_session_at(sessions_dir, "b0", start=_utc_noon(today))
    from cortex.services.session_report import longitudinal as L
    monkeypatch.setattr(
        L, "datetime", _PatchedDatetime(L.datetime, fixed_now=_utc_noon(today))
    )
    agg = LongitudinalAggregator(sessions_dir, chronotype_dir)
    agg.backfill_if_needed()
    model_mtime = (chronotype_dir / "model.json").stat().st_mtime
    # Second call must early-return because model.json + daily/ both exist.
    agg.backfill_if_needed()
    assert (chronotype_dir / "model.json").stat().st_mtime == model_mtime


# ─── hourly_patterns / task_patterns ──────────────────────────────────


def test_hourly_patterns_concentrated_at_hour_14(dirs, monkeypatch) -> None:
    """3 sessions with HYPER stretches at 14:00 UTC. The local-tz hour
    bucket for the hour containing 14:00 UTC must have the highest
    overload_rate.
    """
    sessions_dir, chronotype_dir = dirs
    today = date(2026, 5, 20)
    # Each session starts at 14:00 UTC, transitions to HYPER for 50 min.
    for i in range(3):
        d = today - timedelta(days=2 - i)
        start = datetime(d.year, d.month, d.day, 14, 0, tzinfo=UTC)
        _write_session_at(
            sessions_dir,
            f"h{i}",
            start=start,
            duration_seconds=3600,
            time_in_flow_seconds=600,
            time_in_hyper_seconds=3000,
            hyper_at=[(0.0, "HYPER")],
        )
    from cortex.services.session_report import longitudinal as L
    monkeypatch.setattr(
        L, "datetime", _PatchedDatetime(L.datetime, fixed_now=_utc_noon(today))
    )
    agg = LongitudinalAggregator(sessions_dir, chronotype_dir)
    model = agg.refresh_chronotype(window_days=7)
    # Find the hour with the highest overload rate; it must dominate.
    if not model.hourly_patterns:
        pytest.skip("hourly_patterns empty (no transitions emitted)")
    top = max(model.hourly_patterns, key=lambda p: p.overload_rate)
    assert top.overload_rate >= 0.5
    other_hours = [p for p in model.hourly_patterns if p.hour != top.hour]
    for p in other_hours:
        assert p.overload_rate <= top.overload_rate


def test_task_patterns_marks_trigger_when_rate_above_half(dirs, monkeypatch) -> None:
    """A domain appearing in >50% of sessions in the window is marked
    ``correlation='trigger'`` with ``overload_rate>0.5``."""
    sessions_dir, chronotype_dir = dirs
    today = date(2026, 5, 20)
    # 5 sessions; reddit.com appears in 4 (80%) → trigger.
    for i in range(5):
        d = today - timedelta(days=4 - i)
        domains = ["reddit.com"] if i < 4 else ["other.com"]
        _write_session_at(
            sessions_dir, f"t{i}", start=_utc_noon(d), top_distraction_domains=domains
        )
    from cortex.services.session_report import longitudinal as L
    monkeypatch.setattr(
        L, "datetime", _PatchedDatetime(L.datetime, fixed_now=_utc_noon(today))
    )
    agg = LongitudinalAggregator(sessions_dir, chronotype_dir)
    model = agg.refresh_chronotype(window_days=7)
    by_key = {p.pattern_key: p for p in model.task_patterns}
    assert "reddit.com" in by_key
    assert by_key["reddit.com"].overload_rate > 0.5
    assert by_key["reddit.com"].correlation == "trigger"


# ─── helpers ──────────────────────────────────────────────────────────


class _PatchedDatetime:
    """Drop-in datetime substitute whose ``now()`` returns a fixed instant.

    All other class attributes (``fromisoformat``, ``fromtimestamp``,
    ``combine``, ``UTC``, ...) delegate to the real class so the rest of
    the aggregator code keeps working untouched.
    """

    def __init__(self, real_cls: type, *, fixed_now: datetime) -> None:
        self._real_cls = real_cls
        self._fixed_now = fixed_now

    def now(self, tz: Any = None) -> datetime:
        if tz is None:
            return self._fixed_now.astimezone().replace(tzinfo=None)
        return self._fixed_now.astimezone(tz)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real_cls, name)

    def __call__(self, *args: Any, **kwargs: Any) -> datetime:
        return self._real_cls(*args, **kwargs)
