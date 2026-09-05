"""D5 / D9 / D13 — SQLite migration ledger, loop-thread guard, research retention.

D5: a packaged migration whose sha256 differs from the applied ledger row
used to raise ``StorageCorruptionError`` at startup, so any whitespace
edit to a shipped ``.sql`` bricked every existing install. It is now a
logged compatibility warning; the packaged files are pinned here so an
accidental edit is caught in CI instead. An upgrade fixture walks a real
``user_version=1`` database through ``0002.sql`` (backup, prune, ledger).

D9: ``call_sync``/``transaction_sync`` refuse to run on a thread that is
running an asyncio loop; ``close()`` joins the worker off-loop.

D13: ``policy_mode='research_randomized'`` decisions survive the
operational policy retention sweep.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sqlite3
import threading
from importlib import resources
from pathlib import Path
from uuid import UUID

import pytest

from cortex.application.clock import FakeClock
from cortex.storage import (
    CORTEX_APPLICATION_ID,
    CURRENT_SCHEMA_VERSION,
    SQLiteDatabase,
    StorageCorruptionError,
    StorageError,
)
from cortex.storage.event_writer import BoundedAnalyticsWriter
from cortex.storage.maintenance import RESEARCH_POLICY_MODE, StorageMaintenance

# Pinned sha256 of the packaged migrations. If a test here fails because
# you edited a shipped .sql file: existing installs keep the schema those
# files produced, so (a) add a NEW migration instead of editing a shipped
# one, or (b) if the edit is genuinely cosmetic, update the pin and note
# that installs will log a compatibility warning on next start.
PINNED_MIGRATION_SHA256 = {
    1: "15fd3e3538e00780129d2fdec5b0d255107262153b64222db637ae6ff5e4f00c",
    2: "2394acb17f10b7c55953837cdc925af709bb9a0b8c7fee9c94be4a479161d6e1",
}

_BOOT_ID = UUID("00000000-0000-0000-0000-000000000909")


def _clock(wall_unix_ms: int = 1_900_000_000_000) -> FakeClock:
    return FakeClock(wall_unix_ms=wall_unix_ms, mono_ns=7_000_000_000, _boot_id=_BOOT_ID)


def _decision_row(decision_id: str, *, occurred_at_unix_ms: int, policy_mode: str | None) -> str:
    columns = (
        "decision_id, decision_point_id, policy_name, policy_version, policy_state_sha256, "
        "selected_arm, selected_probability, eligible, available, occurred_at_unix_ms, "
        "occurred_at_mono_ns, boot_id, payload_json, payload_sha256"
    )
    values = (
        f"'{decision_id}', 'dp-{decision_id}', 'policy', 'v1', '{'a' * 64}', 'arm', 0.5, 1, 1, "
        f"{occurred_at_unix_ms}, 1, '{_BOOT_ID}', '{{}}', '{'b' * 64}'"
    )
    if policy_mode is not None:
        columns += ", policy_mode"
        values += f", '{policy_mode}'"
    return f"INSERT INTO policy_decisions({columns}) VALUES ({values})"


# ---------------------------------------------------------------------------
# D5
# ---------------------------------------------------------------------------


def test_packaged_migration_checksums_are_pinned(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "unused.sqlite3")
    assert set(PINNED_MIGRATION_SHA256) == set(range(1, CURRENT_SCHEMA_VERSION + 1))
    for version, expected in PINNED_MIGRATION_SHA256.items():
        name, source, sha = database._read_migration(version)
        assert sha == expected, f"{name} changed on disk; see the note above the pin table"
        raw = resources.files("cortex.storage.migrations").joinpath(name).read_bytes()
        assert hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest() == expected


@pytest.mark.asyncio
async def test_checksum_mismatch_on_applied_version_is_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "cortex.sqlite3"
    first = SQLiteDatabase(path, clock=_clock())
    await first.start()
    await first.close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE schema_migrations SET source_sha256=? WHERE version=?",
            ("f" * 64, CURRENT_SCHEMA_VERSION),
        )

    reopened = SQLiteDatabase(path, clock=_clock())
    with caplog.at_level(logging.WARNING, logger="cortex.storage.database"):
        await reopened.start()
    try:
        notices = [r for r in caplog.records if "compatibility" in r.getMessage()]
        assert notices, "expected a compatibility warning for the edited migration"
        assert (await reopened.health(full_integrity_check=True))["healthy"] is True
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_ledger_count_mismatch_still_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "cortex.sqlite3"
    first = SQLiteDatabase(path, clock=_clock())
    await first.start()
    await first.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version=?", (CURRENT_SCHEMA_VERSION,))

    reopened = SQLiteDatabase(path, clock=_clock())
    with pytest.raises(StorageCorruptionError, match="ledger"):
        await reopened.start()
    await reopened.close()


@pytest.mark.asyncio
async def test_upgrade_from_schema_v1_backs_up_prunes_and_ledgers(tmp_path: Path) -> None:
    path = tmp_path / "cortex.sqlite3"
    helper = SQLiteDatabase(path)
    name_v1, source_v1, sha_v1 = helper._read_migration(1)

    # Build a genuine schema-1 database the way the 0001 migration did.
    with sqlite3.connect(path, isolation_level=None) as connection:
        connection.execute(f"PRAGMA application_id={CORTEX_APPLICATION_ID}")
        for statement in SQLiteDatabase._migration_statements(source_v1):
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations(version, name, source_sha256, applied_at_unix_ms) "
            "VALUES (1, ?, ?, 1)",
            (name_v1, sha_v1),
        )
        connection.execute(_decision_row("legacy-1", occurred_at_unix_ms=5, policy_mode=None))
        connection.execute(
            "INSERT INTO policy_rewards(decision_id, reward_version, reward_value, "
            "finalized_at_unix_ms, components_json, payload_sha256) "
            "VALUES ('legacy-1', 'v1', 0.25, 6, '{}', ?)",
            ("c" * 64,),
        )
        connection.execute("PRAGMA user_version=1")
    path.chmod(0o600)

    # Three stale pre-schema backups already exist; retention keeps three.
    backup_dir = tmp_path / "migration-backups"
    backup_dir.mkdir()
    stale: list[Path] = []
    for index in range(3):
        candidate = backup_dir / f"cortex.pre-schema-1.{100 + index}.sqlite3"
        candidate.write_bytes(b"stale backup")
        os.utime(candidate, (1_000 + index, 1_000 + index))
        stale.append(candidate)

    clock = _clock()
    database = SQLiteDatabase(path, clock=clock)
    await database.start()
    try:
        version, ledger = await database.read(
            lambda connection: (
                int(connection.execute("PRAGMA user_version").fetchone()[0]),
                [
                    (int(row[0]), str(row[1]), str(row[2]))
                    for row in connection.execute(
                        "SELECT version, name, source_sha256 FROM schema_migrations ORDER BY version"
                    )
                ],
            )
        )
        assert version == CURRENT_SCHEMA_VERSION == 2
        assert ledger == [
            (1, "0001_initial.sql", PINNED_MIGRATION_SHA256[1]),
            (2, "0002.sql", PINNED_MIGRATION_SHA256[2]),
        ]
        mode, reward = await database.read(
            lambda connection: (
                str(
                    connection.execute(
                        "SELECT policy_mode FROM policy_decisions WHERE decision_id='legacy-1'"
                    ).fetchone()[0]
                ),
                float(
                    connection.execute(
                        "SELECT reward_value FROM policy_rewards WHERE decision_id='legacy-1'"
                    ).fetchone()[0]
                ),
            )
        )
        assert mode == "legacy_diagnostic"
        assert reward == 0.25
    finally:
        await database.close()

    backups = sorted(backup_dir.glob("cortex.pre-schema-*.sqlite3"))
    assert len(backups) == 3, backups
    assert not stale[0].exists(), "oldest stale backup must be pruned"
    fresh = [b for b in backups if b.name == f"cortex.pre-schema-1.{clock.unix_ms()}.sqlite3"]
    assert len(fresh) == 1, backups
    assert fresh[0].stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(fresh[0]) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert int(backup.execute("PRAGMA user_version").fetchone()[0]) == 1
        assert backup.execute("SELECT COUNT(*) FROM policy_rewards").fetchone()[0] == 1

    # And the upgraded database re-opens cleanly (ledger verified).
    again = SQLiteDatabase(path, clock=clock)
    await again.start()
    await again.close()


# ---------------------------------------------------------------------------
# D13
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_research_randomized_decisions_survive_policy_retention(tmp_path: Path) -> None:
    clock = _clock(wall_unix_ms=200 * 86_400_000)
    database = SQLiteDatabase(tmp_path / "cortex.sqlite3", clock=clock)
    ancient = 1_000  # far older than any retention window

    def seed(connection: sqlite3.Connection) -> None:
        connection.execute(
            _decision_row("ordinary-1", occurred_at_unix_ms=ancient, policy_mode="deterministic")
        )
        connection.execute(
            _decision_row(
                "research-1", occurred_at_unix_ms=ancient, policy_mode=RESEARCH_POLICY_MODE
            )
        )
        connection.execute(
            _decision_row("legacy-1", occurred_at_unix_ms=ancient, policy_mode=None)
        )

    await database.transaction(seed)
    maintenance = StorageMaintenance(
        database,
        storage_root=tmp_path,
        analytics_writer=BoundedAnalyticsWriter(database),
        clock=clock,
        retention_days={"sessions": 180, "policy": 90, "interventions": 90},
    )
    deleted = await maintenance.enforce_retention()
    assert deleted["policy"] == 2
    remaining = await database.read(
        lambda connection: sorted(
            str(row[0]) for row in connection.execute("SELECT decision_id FROM policy_decisions")
        )
    )
    assert remaining == ["research-1"]

    # An explicit user erase still removes everything.
    erased, _ = await maintenance.delete(("policy",))
    assert erased["policy"] == 1
    await database.close()


# ---------------------------------------------------------------------------
# D9
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_bridge_rejects_event_loop_thread(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "cortex.sqlite3", clock=_clock())
    await database.start()
    try:
        with pytest.raises(StorageError, match="event loop"):
            database.call_sync(lambda connection: 1)
        with pytest.raises(StorageError, match="event loop"):
            database.transaction_sync(lambda connection: 1)
        # Dispatched to a worker thread the same call is fine.
        assert (
            await asyncio.to_thread(
                database.call_sync,
                lambda connection: int(connection.execute("SELECT 41 + 1").fetchone()[0]),
            )
            == 42
        )
    finally:
        await database.close()


def test_sync_bridge_works_without_a_running_loop(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "cortex.sqlite3", clock=_clock())
    try:
        assert database.call_sync(lambda connection: 7) == 7
    finally:
        asyncio.run(database.close())


@pytest.mark.asyncio
async def test_close_joins_worker_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = SQLiteDatabase(tmp_path / "cortex.sqlite3", clock=_clock())
    await database.start()
    original = database._executor.shutdown
    joined_on: list[int] = []

    def spy(wait: bool = True, *, cancel_futures: bool = False) -> None:
        joined_on.append(threading.get_ident())
        original(wait=wait, cancel_futures=cancel_futures)

    monkeypatch.setattr(database._executor, "shutdown", spy)
    await database.close()
    assert joined_on, "close() must still join the worker"
    assert joined_on[0] != threading.get_ident(), "the join must not run on the loop thread"
    assert database.closed is True
