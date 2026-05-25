"""
LongitudinalAggregator resilience tests (P0 §3.2).

Verifies the aggregator survives partial / hostile inputs without
raising:

* A corrupt session JSON in the sessions dir doesn't crash
  ``aggregate_day`` or ``refresh_chronotype``.
* A corrupt ``chronotype/daily/<date>.json`` doesn't crash
  ``get_trends`` (the file is skipped, model is recomputed if stale).
* An empty sessions dir + empty chronotype dir yields a valid empty
  envelope.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cortex.services.session_report.longitudinal import LongitudinalAggregator
from cortex.services.session_report.models import SessionReport


def _utc_noon(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 12, 0, tzinfo=UTC)


def _write_session(
    sessions_dir: Path, session_id: str, *, start: datetime
) -> None:
    report = SessionReport(
        session_id=session_id,
        start_time=start,
        end_time=start + timedelta(seconds=600),
        duration_seconds=600.0,
        time_in_flow_seconds=400.0,
        time_in_hyper_seconds=60.0,
        avg_hr_bpm=72.0,
        avg_hrv_rmssd=50.0,
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


class _PatchedDatetime:
    """``datetime`` shim whose ``now()`` returns a fixed instant."""

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


def _freeze(monkeypatch, day: date) -> None:
    from cortex.services.session_report import longitudinal as L

    monkeypatch.setattr(
        L, "datetime", _PatchedDatetime(L.datetime, fixed_now=_utc_noon(day))
    )


def test_corrupt_session_does_not_crash_aggregate_day(dirs, caplog) -> None:
    """A literal ``not json`` session file is skipped, valid siblings are aggregated."""
    sessions_dir, chronotype_dir = dirs
    d = date(2026, 5, 20)
    _write_session(sessions_dir, "good", start=_utc_noon(d))
    (sessions_dir / "session_corrupt.json").write_text("not json", encoding="utf-8")
    caplog.set_level("WARNING")
    agg = LongitudinalAggregator(sessions_dir, chronotype_dir)
    baseline = agg.aggregate_day(d)
    assert baseline.session_count == 1
    # We expect a warning log mentioning malformed JSON / longitudinal.
    assert any(
        "malformed JSON" in r.message or "longitudinal" in r.message
        for r in caplog.records
    )


def test_corrupt_session_does_not_crash_refresh_chronotype(dirs, monkeypatch) -> None:
    sessions_dir, chronotype_dir = dirs
    d = date(2026, 5, 20)
    _write_session(sessions_dir, "good", start=_utc_noon(d))
    (sessions_dir / "session_corrupt.json").write_text("{not", encoding="utf-8")
    _freeze(monkeypatch, d)
    agg = LongitudinalAggregator(sessions_dir, chronotype_dir)
    model = agg.refresh_chronotype(window_days=7)
    # Successfully wrote a model based on the good session.
    assert len(model.baselines) == 1


def test_corrupt_daily_baseline_does_not_crash_get_trends(dirs, monkeypatch) -> None:
    """A junk daily file is skipped, ``get_trends`` returns the rest."""
    sessions_dir, chronotype_dir = dirs
    today = date(2026, 5, 20)
    _write_session(sessions_dir, "g", start=_utc_noon(today))
    _freeze(monkeypatch, today)
    agg = LongitudinalAggregator(sessions_dir, chronotype_dir)
    # Prime the rollups so daily/ exists.
    agg.refresh_chronotype(window_days=7)
    # Drop a corrupt daily file inside the window.
    daily_dir = chronotype_dir / "daily"
    (daily_dir / "2026-05-19.json").write_text("not json", encoding="utf-8")
    # Should not raise.
    trends = agg.get_trends("week")
    # The corrupt file is skipped; the valid one (today) is still present.
    dates = {b.record_date for b in trends.daily}
    assert today in dates


def test_empty_state_returns_valid_envelope(dirs, monkeypatch) -> None:
    sessions_dir, chronotype_dir = dirs
    # Both dirs empty.
    _freeze(monkeypatch, date(2026, 5, 20))
    agg = LongitudinalAggregator(sessions_dir, chronotype_dir)
    trends = agg.get_trends("week")
    assert trends.window == "week"
    assert trends.daily == []
    # ChronotypeModel default constructed; no exception.
    assert trends.chronotype is not None
