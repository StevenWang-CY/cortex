"""The cost ledger's day key follows the local offset of each instant.

``test_clock_contracts`` once built an August instant with today's fixed
offset and failed every winter. These tests pin a daylight-saving zone and
check the production day roll on both sides of the year, so the class of
error (a fixed offset borrowed from "now") cannot hide in ``_local_date``.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from cortex.application.clock import FakeClock
from cortex.services.llm_engine.cost_tracker import CostTracker, _today_iso

_ZONE = "America/New_York"


@pytest.fixture
def new_york_local_time(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is unavailable on this platform")
    monkeypatch.setenv("TZ", _ZONE)
    time.tzset()
    try:
        yield
    finally:
        monkeypatch.undo()
        time.tzset()


def _unix_ms(value: datetime) -> int:
    return int(value.timestamp() * 1_000)


@pytest.mark.parametrize(
    ("year", "month", "day"),
    [(2026, 8, 24), (2026, 1, 24)],
    ids=["daylight-time", "standard-time"],
)
def test_ledger_rolls_at_the_local_midnight_of_the_recorded_season(
    tmp_path: Path,
    new_york_local_time: None,
    year: int,
    month: int,
    day: int,
) -> None:
    before = datetime(year, month, day, 23, 59, 59, tzinfo=ZoneInfo(_ZONE))
    clock = FakeClock(wall_unix_ms=_unix_ms(before.astimezone(UTC)), mono_ns=0)
    tracker = CostTracker(tmp_path / "cost.json", clock=clock)

    tracker.record("a", "model", 1.0)
    assert tracker.today_total_usd() == pytest.approx(1.0)

    clock.advance(wall_ms=2_000, monotonic_ns=2_000_000_000)
    tracker.record("b", "model", 2.0)

    assert tracker.today_total_usd() == pytest.approx(2.0)
    assert sorted(tracker._days) == [
        f"{year:04d}-{month:02d}-{day:02d}",
        f"{year:04d}-{month:02d}-{day + 1:02d}",
    ]


def test_day_key_uses_each_instants_offset_not_todays(new_york_local_time: None) -> None:
    # 04:30 UTC falls between the two seasons' midnights: 00:30 EDT in
    # August, 23:30 EST in January. A fixed offset borrowed from either
    # season files one of the two on the wrong day.
    summer_edge = datetime(2026, 8, 25, 4, 30, tzinfo=UTC)  # 00:30 EDT
    winter_edge = datetime(2026, 1, 25, 4, 30, tzinfo=UTC)  # 23:30 EST
    assert _today_iso(summer_edge) == "2026-08-25"
    assert _today_iso(winter_edge) == "2026-01-24"
