"""D6 — session retention defaults and the size budget.

* The storage budget only counted ``storage/sessions/*.json``; the raw
  telemetry ``*.jsonl`` (by far the largest artefact) was invisible to it.
  ``cortex.services.janitor.retention.enforce_session_storage_budget``
  counts both and evicts oldest-first across them.
* ``LegacyDataMigrator`` and ``StorageMaintenance`` fell back to a 7-day
  session retention when the caller did not thread the configured value
  through; the in-code default is now 180 days (the ``settings.py``
  default is a patch note for the config owner).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from cortex.application.clock import FakeClock
from cortex.libs.schemas.session_report import SessionReport
from cortex.services.janitor.retention import (
    SESSION_BUDGET_SUFFIXES,
    enforce_session_storage_budget,
)
from cortex.storage import DEFAULT_SESSION_RETENTION_DAYS, SQLiteDatabase
from cortex.storage.event_writer import BoundedAnalyticsWriter
from cortex.storage.legacy_migrator import LegacyDataMigrator
from cortex.storage.maintenance import StorageMaintenance

_DAY_MS = 86_400_000


def _write(directory: Path, name: str, kb: int, mtime: float) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"x" * (kb * 1024))
    os.utime(path, (mtime, mtime))
    return path


def test_budget_counts_jsonl_telemetry_and_evicts_oldest_first(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    a_json = _write(sessions, "session_a.json", 100, 1_000.0)
    a_jsonl = _write(sessions, "session_a.jsonl", 400, 1_000.0)
    b_json = _write(sessions, "session_b.json", 100, 2_000.0)
    b_jsonl = _write(sessions, "session_b.jsonl", 400, 2_000.0)
    digest = _write(sessions, "session_b.md", 50, 2_000.0)  # not budgeted

    assert SESSION_BUDGET_SUFFIXES == {".json", ".jsonl"}

    # 1 000 KB on disk + 600 KB incoming against a 1 MB cap: the .json-only
    # accounting would have seen 200 KB and evicted nothing.
    evicted = enforce_session_storage_budget(
        sessions, incoming_bytes=600 * 1024, max_total_size_mb=1
    )
    assert evicted == 3
    assert not a_json.exists()
    assert not a_jsonl.exists()
    assert not b_json.exists()
    assert b_jsonl.exists(), "eviction stops as soon as the write fits"
    assert digest.exists(), "non-budgeted artefacts are never touched"


def test_budget_zero_evicts_every_session_file(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    files = [
        _write(sessions, f"session_{i}.{suffix}", 4, 1_000.0 + i)
        for i, suffix in enumerate(("json", "jsonl", "json", "jsonl"))
    ]
    assert enforce_session_storage_budget(sessions, incoming_bytes=1, max_total_size_mb=0) == 4
    assert not any(path.exists() for path in files)


def test_budget_missing_directory_and_negative_cap_are_noops(tmp_path: Path) -> None:
    assert enforce_session_storage_budget(tmp_path / "nope", incoming_bytes=1, max_total_size_mb=1) == 0
    sessions = tmp_path / "sessions"
    kept = _write(sessions, "session_k.jsonl", 8, 1_000.0)
    assert enforce_session_storage_budget(sessions, incoming_bytes=10**9, max_total_size_mb=-1) == 0
    assert kept.exists()


def _clock() -> FakeClock:
    return FakeClock(
        wall_unix_ms=1_900_000_000_000,
        mono_ns=7_000_000_000,
        _boot_id=UUID("00000000-0000-0000-0000-000000000606"),
    )


def _report(session_id: str, *, ended: datetime) -> SessionReport:
    return SessionReport(
        session_id=session_id,
        start_time=ended - timedelta(minutes=30),
        end_time=ended,
        duration_seconds=1_800,
        time_in_flow_seconds=900,
        interventions_triggered=1,
        interventions_accepted=1,
    )


@pytest.mark.asyncio
async def test_legacy_migrator_default_expiry_is_180_days(tmp_path: Path) -> None:
    assert DEFAULT_SESSION_RETENTION_DAYS == 180
    database = SQLiteDatabase(tmp_path / "cortex.sqlite3", clock=_clock())
    migrator = LegacyDataMigrator(database, storage_root=tmp_path, clock=_clock())
    ended = datetime(2026, 1, 1, 12, tzinfo=UTC)
    await migrator.upsert_session(_report("s-default", ended=ended))
    ended_ms, expires_ms = await database.read(
        lambda connection: connection.execute(
            "SELECT ended_at_unix_ms, expires_at_unix_ms FROM session_aggregates "
            "WHERE session_id='s-default'"
        ).fetchone()
    )
    assert int(expires_ms) - int(ended_ms) == DEFAULT_SESSION_RETENTION_DAYS * _DAY_MS
    await database.close()


@pytest.mark.asyncio
async def test_maintenance_default_session_retention_is_180_days(tmp_path: Path) -> None:
    clock = _clock()
    now = datetime.fromtimestamp(clock.unix_ms() / 1_000, tz=UTC)
    database = SQLiteDatabase(tmp_path / "cortex.sqlite3", clock=clock)
    migrator = LegacyDataMigrator(database, storage_root=tmp_path, clock=clock)
    await migrator.upsert_session(_report("recent", ended=now - timedelta(days=100)))
    await migrator.upsert_session(_report("ancient", ended=now - timedelta(days=200)))

    maintenance = StorageMaintenance(
        database,
        storage_root=tmp_path,
        analytics_writer=BoundedAnalyticsWriter(database),
        clock=clock,
        retention_days={},  # nothing configured → in-code default
    )
    deleted = await maintenance.enforce_retention()
    assert deleted["sessions"] == 1
    remaining = await database.read(
        lambda connection: sorted(
            str(row[0]) for row in connection.execute("SELECT session_id FROM session_aggregates")
        )
    )
    assert remaining == ["recent"]
    await database.close()
