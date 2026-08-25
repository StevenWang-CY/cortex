"""WP7 transactional SQLite, migration, maintenance, and backpressure gates."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from cortex.application.clock import FakeClock
from cortex.libs.schemas.calibration import (
    CalibrationBaselineValues,
    CalibrationCameraIdentity,
    CalibrationDistribution,
    CalibrationMetricMaturity,
    CalibrationMetricName,
    CalibrationMetricSummary,
    CalibrationProfile,
    CalibrationProvenance,
    CalibrationReferenceTask,
)
from cortex.libs.schemas.intervention import InterventionPlan, SuggestedAction, UIPlan
from cortex.libs.schemas.physiology import SignalAlgorithmIdentity
from cortex.libs.schemas.session_report import SessionReport
from cortex.libs.schemas.storage import StoredAnalyticsEvent
from cortex.services.capture_service.calibration_store import CalibrationProfileStore
from cortex.services.consent.ladder import ConsentLadder
from cortex.services.consent.policy import ConsentPolicy
from cortex.services.intervention_engine.transaction import (
    InterventionTransactionCoordinator,
    build_action_manifest,
)
from cortex.storage import (
    CORTEX_APPLICATION_ID,
    CURRENT_SCHEMA_VERSION,
    SQLiteDatabase,
    StorageCapacityError,
    StorageCompatibilityError,
    StorageCorruptionError,
    StorageReadOnlyError,
)
from cortex.storage.event_writer import BoundedAnalyticsWriter
from cortex.storage.intervention_store import SQLiteInterventionTransactionStore
from cortex.storage.key_value_store import SQLiteKeyValueStore
from cortex.storage.legacy_migrator import LegacyDataMigrator
from cortex.storage.maintenance import ActiveInterventionDataError, StorageMaintenance

_BOOT_ID = UUID("00000000-0000-0000-0000-000000000707")


def _clock() -> FakeClock:
    return FakeClock(
        wall_unix_ms=1_900_000_000_000,
        mono_ns=7_000_000_000,
        _boot_id=_BOOT_ID,
    )


def _database(root: Path, *, clock: FakeClock | None = None) -> SQLiteDatabase:
    return SQLiteDatabase(root / "cortex.sqlite3", clock=clock or _clock())


def _plan() -> InterventionPlan:
    return InterventionPlan(
        intervention_id="int_sqlite_restart",
        level="overlay_only",
        situation_summary="A focused reference may help.",
        headline="Open the exact reference",
        primary_focus="Continue the current task",
        micro_steps=["Review the proposed reference"],
        ui_plan=UIPlan(show_overlay=True, intervention_type="overlay_only"),
        suggested_actions=[
            SuggestedAction(
                action_id="open_reference",
                action_type="open_url",
                target="https://example.com/reference",
                label="Open reference",
                reason="Keep the exact reference nearby",
                reversible=True,
            )
        ],
        consent_level="preview",
    )


def _calibration_profile() -> CalibrationProfile:
    value = 14.0
    return CalibrationProfile(
        profile_id=UUID("00000000-0000-0000-0000-000000000777"),
        provenance=CalibrationProvenance.MEASURED,
        created_at_unix_ms=1_700_000_000_000,
        approved_at_unix_ms=1_700_000_001_000,
        feature_schema_version="features/2.0",
        protocol_version="calibration/2.0",
        camera=CalibrationCameraIdentity(
            identity_key="built-in:test-camera",
            device_name="Test Camera",
            source="builtin",
            width=1280,
            height=720,
        ),
        metrics=(
            CalibrationMetricSummary(
                metric=CalibrationMetricName.BLINK_RATE_PER_MIN,
                unit="blinks/min",
                reference_task=CalibrationReferenceTask.REPRESENTATIVE_WORK,
                maturity=CalibrationMetricMaturity.OBSERVED,
                value=value,
                distribution=CalibrationDistribution(
                    mean=value,
                    std=1.0,
                    p10=12.0,
                    median=value,
                    p90=16.0,
                ),
                sample_count=30,
                effective_sample_count=20.0,
                valid_duration_seconds=30.0,
                missing_fraction=0.0,
                quality_p10=0.7,
                quality_median=0.8,
                quality_p90=0.9,
                algorithm=SignalAlgorithmIdentity(
                    name="kinematics",
                    version="2.0.0",
                    implementation_sha256="a" * 64,
                    configuration_sha256="b" * 64,
                    selection_mode="fixed",
                ),
            ),
        ),
        baselines=CalibrationBaselineValues(blink_rate_per_min=value),
    )


@pytest.mark.asyncio
async def test_database_enforces_rollback_journal_integrity_and_permissions(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    await database.start()
    health = await database.health(full_integrity_check=True)
    assert health["healthy"] is True
    assert health["journal_mode"] == "delete"
    assert health["synchronous"] == "full"
    assert health["foreign_keys"] is True
    assert health["schema_version"] == CURRENT_SCHEMA_VERSION
    assert database.path.stat().st_mode & 0o777 == 0o600
    assert database.path.parent.stat().st_mode & 0o777 == 0o700
    application_id = await database.read(
        lambda connection: int(connection.execute("PRAGMA application_id").fetchone()[0])
    )
    assert application_id == CORTEX_APPLICATION_ID

    backup = await database.backup(tmp_path / "manual-backup.sqlite3")
    assert backup.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    await database.close()


@pytest.mark.asyncio
async def test_database_rejects_broad_or_symlinked_storage_targets(
    tmp_path: Path,
) -> None:
    broad = SQLiteDatabase(Path("/tmp") / "cortex-must-not-create.sqlite3")
    with pytest.raises(StorageCompatibilityError, match="dedicated"):
        await broad.start()
    assert not broad.path.exists()
    await broad.close()

    target = tmp_path / "target.sqlite3"
    first = SQLiteDatabase(target)
    await first.start()
    await first.close()
    target.chmod(0o644)
    link = tmp_path / "linked.sqlite3"
    link.symlink_to(target)
    linked = SQLiteDatabase(link)
    with pytest.raises(StorageCompatibilityError, match="symbolic link"):
        await linked.start()
    assert target.stat().st_mode & 0o777 == 0o644
    await linked.close()

    reopened = SQLiteDatabase(target)
    await reopened.start()
    assert target.stat().st_mode & 0o777 == 0o600
    await reopened.close()


@pytest.mark.asyncio
async def test_transaction_rolls_back_python_and_disk_full_faults(tmp_path: Path) -> None:
    database = _database(tmp_path)
    await database.start()

    def python_fault(connection: sqlite3.Connection) -> None:
        connection.execute(
            "INSERT INTO key_values(namespace, key, value_kind, value_json, "
            "value_sha256, updated_at_unix_ms) "
            "VALUES ('cortex', 'partial', 'integer', '1', ?, 1)",
            ("6b86b273ff34fce19d6b804eff5a3f5747ada4eaa22f1d49c01e52ddb7875b4b",),
        )
        raise RuntimeError("fault after write before commit")

    with pytest.raises(RuntimeError, match="before commit"):
        await database.transaction(python_fault)
    assert (
        await database.read(
            lambda connection: connection.execute(
                "SELECT COUNT(*) FROM key_values WHERE key='partial'"
            ).fetchone()[0]
        )
        == 0
    )

    def disk_full(connection: sqlite3.Connection) -> None:
        connection.execute(
            "INSERT INTO key_values(namespace, key, value_kind, value_json, "
            "value_sha256, updated_at_unix_ms) "
            "VALUES ('cortex', 'full', 'integer', '1', ?, 1)",
            ("6b86b273ff34fce19d6b804eff5a3f5747ada4eaa22f1d49c01e52ddb7875b4b",),
        )
        raise OSError(errno.ENOSPC, "synthetic disk full")

    with pytest.raises(StorageCapacityError):
        await database.transaction(disk_full)
    assert (
        await database.read(
            lambda connection: connection.execute(
                "SELECT COUNT(*) FROM key_values WHERE key='full'"
            ).fetchone()[0]
        )
        == 0
    )
    await database.close()


@pytest.mark.asyncio
async def test_future_and_corrupt_databases_fail_closed(tmp_path: Path) -> None:
    future_path = tmp_path / "future.sqlite3"
    first = SQLiteDatabase(future_path)
    await first.start()
    await first.close()
    with sqlite3.connect(future_path) as connection:
        connection.execute("PRAGMA user_version=999")
    future = SQLiteDatabase(future_path)
    with pytest.raises(StorageCompatibilityError, match="newer"):
        await future.start()
    await future.close()

    corrupt_path = tmp_path / "corrupt.sqlite3"
    corrupt_path.write_bytes(b"not a sqlite database\x00authority bytes retained")
    before = corrupt_path.read_bytes()
    corrupt = SQLiteDatabase(corrupt_path)
    with pytest.raises(StorageCorruptionError):
        await corrupt.start()
    assert corrupt_path.read_bytes() == before
    await corrupt.close()


@pytest.mark.asyncio
async def test_read_only_database_fails_startup_write_probe(tmp_path: Path) -> None:
    path = tmp_path / "readonly.sqlite3"
    first = SQLiteDatabase(path)
    await first.start()
    await first.close()
    path.chmod(0o400)
    readonly = SQLiteDatabase(path)
    try:
        with pytest.raises(StorageReadOnlyError):
            await readonly.start()
    finally:
        path.chmod(0o600)
        await readonly.close()


@pytest.mark.asyncio
async def test_sigkill_mid_transaction_recovers_hot_rollback_journal(
    tmp_path: Path,
) -> None:
    path = tmp_path / "power-loss.sqlite3"
    initial = SQLiteDatabase(path)
    await initial.start()
    await initial.close()
    script = """
import sqlite3, sys, time
path = sys.argv[1]
connection = sqlite3.connect(path, isolation_level=None)
connection.execute('PRAGMA journal_mode=DELETE')
connection.execute('PRAGMA synchronous=FULL')
connection.execute('BEGIN IMMEDIATE')
connection.execute(
    \"INSERT INTO key_values(namespace,key,value_kind,value_json,value_sha256,updated_at_unix_ms) \"
    \"VALUES ('cortex','uncommitted','integer','1',\"
    \"'6b86b273ff34fce19d6b804eff5a3f5747ada4eaa22f1d49c01e52ddb7875b4b',1)\"
)
print('ready', flush=True)
time.sleep(60)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "ready"
    process.kill()
    process.wait(timeout=5)

    recovered = SQLiteDatabase(path)
    await recovered.start()
    assert (
        await recovered.read(
            lambda connection: connection.execute(
                "SELECT COUNT(*) FROM key_values WHERE key='uncommitted'"
            ).fetchone()[0]
        )
        == 0
    )
    assert (await recovered.health(full_integrity_check=True))["healthy"] is True
    await recovered.close()


@pytest.mark.asyncio
async def test_sqlite_transaction_journal_survives_restart_and_checks_projection(
    tmp_path: Path,
) -> None:
    clock = _clock()
    database = _database(tmp_path, clock=clock)
    store = SQLiteInterventionTransactionStore(database, clock=clock)
    policy = ConsentPolicy()
    ladder = ConsentLadder(policy=policy, clock=clock)
    coordinator = InterventionTransactionCoordinator(
        ladder,
        store=store,
        clock=clock,
        execution_mode="authorized",
    )
    manifest = build_action_manifest(
        _plan(),
        [],
        consent_policy=policy,
        clock=clock,
    )
    await coordinator.register_proposal(manifest)
    await coordinator.mark_delivered(manifest.intervention_id)
    original = await store.load()
    assert original.transactions[manifest.intervention_id].state == "delivered"
    await database.close()

    reopened = _database(tmp_path, clock=clock)
    reopened_store = SQLiteInterventionTransactionStore(reopened, clock=clock)
    recovered = await reopened_store.load()
    assert recovered == original
    await reopened.transaction(
        lambda connection: connection.execute(
            "UPDATE intervention_transitions SET payload_sha256=? "
            "WHERE intervention_id=? AND ordinal=0",
            ("f" * 64, manifest.intervention_id),
        )
    )
    with pytest.raises(StorageCorruptionError, match="projection mismatch"):
        await reopened_store.load()
    await reopened.close()


@pytest.mark.asyncio
async def test_legacy_intervention_import_is_backed_up_and_idempotent(
    tmp_path: Path,
) -> None:
    clock = _clock()
    source_db = _database(tmp_path / "source", clock=clock)
    source_store = SQLiteInterventionTransactionStore(source_db, clock=clock)
    policy = ConsentPolicy()
    coordinator = InterventionTransactionCoordinator(
        ConsentLadder(policy=policy, clock=clock),
        store=source_store,
        clock=clock,
        execution_mode="authorized",
    )
    manifest = build_action_manifest(_plan(), [], consent_policy=policy, clock=clock)
    await coordinator.register_proposal(manifest)
    journal = await source_store.load()
    await source_db.close()

    legacy = tmp_path / "intervention_transactions.json"
    legacy.write_text(json.dumps(journal.model_dump(mode="json")), encoding="utf-8")
    database = _database(tmp_path, clock=clock)
    store = SQLiteInterventionTransactionStore(
        database,
        legacy_json_path=legacy,
        clock=clock,
    )
    first = await store.load()
    second = await store.load()
    assert first == second == journal
    ledger_count = await database.read(
        lambda connection: connection.execute(
            "SELECT COUNT(*) FROM legacy_migrations "
            "WHERE source_kind='intervention_transactions_json'"
        ).fetchone()[0]
    )
    assert ledger_count == 1
    backups = list((database.backup_dir / "legacy").glob("*.json"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == legacy.read_bytes()
    await database.close()


@pytest.mark.asyncio
async def test_legacy_key_value_migration_skips_opaque_secret_keys_and_merges_consent(
    tmp_path: Path,
) -> None:
    old_store = tmp_path / "store.json"
    old_store.write_text(
        json.dumps(
            {
                "data": {
                    "cortex:consent_ladder_state": {
                        "action_states": {},
                        "global_max": 3,
                        "revision": 4,
                    },
                    "cortex:api_token": {"token": "must-not-migrate"},
                },
                "expiry": {},
            }
        ),
        encoding="utf-8",
    )
    overrides = tmp_path / "consent_overrides.json"
    overrides.write_text(
        json.dumps({"levels": {"distraction_block": 4}, "global_max": 4}),
        encoding="utf-8",
    )
    database = _database(tmp_path)
    store = SQLiteKeyValueStore(
        database,
        legacy_store_path=old_store,
        legacy_consent_overrides_path=overrides,
    )
    consent = await store.get_json("consent_ladder_state")
    assert consent is not None
    assert consent["revision"] == 4
    assert consent["policy"]["levels"]["distraction_block"] == 4
    assert await store.get_json("api_token") is None
    skipped = await database.read(
        lambda connection: connection.execute(
            "SELECT skipped_records FROM legacy_migrations WHERE source_kind='key_value_json'"
        ).fetchone()[0]
    )
    assert skipped == 1
    maintenance = StorageMaintenance(
        database,
        storage_root=tmp_path,
        analytics_writer=BoundedAnalyticsWriter(database),
        legacy_store_path=old_store,
    )
    deleted, _vacuumed = await maintenance.delete(("consent",))
    assert deleted["consent"] == 1
    assert not overrides.exists()
    legacy_after = json.loads(old_store.read_text(encoding="utf-8"))
    assert "cortex:consent_ladder_state" not in legacy_after["data"]
    assert legacy_after["data"]["cortex:api_token"] == {"token": "must-not-migrate"}
    assert await store.get_json("consent_ladder_state") is None
    await database.close()


@pytest.mark.asyncio
async def test_key_value_ttl_and_atomic_increment_survive_restart(tmp_path: Path) -> None:
    clock = _clock()
    database = _database(tmp_path, clock=clock)
    store = SQLiteKeyValueStore(database, clock=clock)
    await store.set_json("expiring", {"ok": True}, ttl_seconds=1)
    assert await store.get_json("expiring") == {"ok": True}
    values = await asyncio.gather(*(store.increment("counter") for _ in range(20)))
    assert sorted(values) == list(range(1, 21))
    clock.advance(wall_ms=1_000, monotonic_ns=1_000_000_000)
    assert await store.get_json("expiring") is None
    await database.close()

    reopened = _database(tmp_path, clock=clock)
    reopened_store = SQLiteKeyValueStore(reopened, clock=clock)
    assert await reopened_store.increment("counter") == 21
    await reopened.close()


@pytest.mark.asyncio
async def test_maintenance_honors_configured_key_namespace(tmp_path: Path) -> None:
    database = _database(tmp_path)
    store = SQLiteKeyValueStore(database, key_prefix="private-cortex")
    await store.set_json("consent_ladder_state", {"revision": 3})
    maintenance = StorageMaintenance(
        database,
        storage_root=tmp_path,
        analytics_writer=BoundedAnalyticsWriter(database),
        namespace="private-cortex",
    )
    exported = await maintenance.export(("consent",))
    document = json.loads(
        (tmp_path / "exports" / exported.filename).read_text(encoding="utf-8")
    )
    assert document["data"]["consent"] == [{"revision": 3}]
    deleted, _vacuumed = await maintenance.delete(("consent",))
    assert deleted["consent"] == 1
    assert await store.get_json("consent_ladder_state") is None
    await database.close()


@pytest.mark.asyncio
async def test_bounded_writer_drops_on_saturation_and_persists_idempotently(
    tmp_path: Path,
) -> None:
    clock = _clock()
    database = _database(tmp_path, clock=clock)
    writer = BoundedAnalyticsWriter(database, capacity=1, batch_size=1)
    first = StoredAnalyticsEvent.create(
        clock,
        event_type="queue_test",
        aggregate_type="test",
        aggregate_id="a",
        privacy_class="operational",
        payload={"value": 1},
        retention_seconds=60,
    )
    second = StoredAnalyticsEvent.create(
        clock,
        event_type="queue_test",
        aggregate_type="test",
        aggregate_id="b",
        privacy_class="operational",
        payload={"value": 2},
        retention_seconds=60,
    )
    assert writer.offer(first) is True
    assert writer.offer(second) is False
    assert writer.dropped_total == 1
    await writer.start()
    await writer.stop()
    assert writer.persisted_total == 1
    assert (
        await database.read(
            lambda connection: connection.execute(
                "SELECT COUNT(*) FROM analytics_events"
            ).fetchone()[0]
        )
        == 1
    )
    await database.close()


@pytest.mark.asyncio
async def test_unstarted_writer_accounts_for_admitted_events_on_stop(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    writer = BoundedAnalyticsWriter(database, capacity=2, batch_size=2)
    event = StoredAnalyticsEvent.create(
        _clock(),
        event_type="shutdown_test",
        aggregate_type="test",
        aggregate_id=None,
        privacy_class="operational",
        payload={"value": 1},
        retention_seconds=60,
    )
    assert writer.offer(event) is True
    await writer.stop()
    assert writer.queue_depth == 0
    assert writer.dropped_total == 1
    assert database.started is False
    await database.close()


@pytest.mark.asyncio
async def test_session_migration_export_retention_and_active_delete_guard(
    tmp_path: Path,
) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    start = datetime(2026, 1, 1, tzinfo=UTC)
    report = SessionReport(
        session_id="session-safe-id",
        start_time=start,
        end_time=start + timedelta(minutes=30),
        duration_seconds=1_800,
        time_in_flow_seconds=900,
        interventions_triggered=2,
        interventions_accepted=1,
    )
    (sessions / "session_session-safe-id.json").write_text(
        json.dumps(report.model_dump(mode="json")),
        encoding="utf-8",
    )
    clock = _clock()
    database = _database(tmp_path, clock=clock)
    migrator = LegacyDataMigrator(
        database,
        storage_root=tmp_path,
        clock=clock,
        session_retention_days=7,
    )
    await migrator.migrate_all()
    writer = BoundedAnalyticsWriter(database)
    maintenance = StorageMaintenance(
        database,
        storage_root=tmp_path,
        analytics_writer=writer,
        clock=clock,
        retention_days={"sessions": 7, "policy": 90, "interventions": 90},
    )
    export = await maintenance.export(("sessions",))
    export_path = tmp_path / "exports" / export.filename
    assert export_path.exists()
    assert hashlib_sha256(export_path.read_bytes()) == export.sha256
    exported = json.loads(export_path.read_text(encoding="utf-8"))
    assert exported["data"]["sessions"][0]["session_id"] == report.session_id
    assert "goal_title" not in exported["data"]["sessions"][0]

    # Simulate an authority-bearing state at the maintenance boundary. The
    # deletion guard runs before any DELETE and retains the evidence.
    await database.transaction(
        lambda connection: connection.execute(
            "INSERT INTO intervention_transactions("
            "intervention_id, manifest_sha256, lifecycle_state, revision, "
            "created_at_unix_ms, updated_at_unix_ms, aggregate_json, aggregate_sha256"
            ") VALUES ('active', ?, 'applied', 1, 1, 1, '{}', ?)",
            ("a" * 64, hashlib_sha256(b"{}")),
        )
    )
    with pytest.raises(ActiveInterventionDataError):
        await maintenance.delete(("interventions",))
    assert (
        await database.read(
            lambda connection: connection.execute(
                "SELECT COUNT(*) FROM intervention_transactions"
            ).fetchone()[0]
        )
        == 1
    )

    deleted, vacuumed = await maintenance.delete(("sessions",))
    assert deleted["sessions"] == 1
    assert deleted["projection_files"] == 2
    assert not (sessions / "session_session-safe-id.json").exists()
    assert not list((database.backup_dir / "legacy").glob("session_report_json.*"))
    assert vacuumed is True
    await database.close()


@pytest.mark.asyncio
async def test_valid_calibration_migrates_as_authority_and_delete_clears_projections(
    tmp_path: Path,
) -> None:
    profile = _calibration_profile()
    projection_store = CalibrationProfileStore(tmp_path, clock=_clock())
    projection_store.activate(profile)
    database = _database(tmp_path)
    migrator = LegacyDataMigrator(database, storage_root=tmp_path, clock=_clock())
    await migrator.migrate_all()
    assert await migrator.load_active_calibration() == profile

    writer = BoundedAnalyticsWriter(database)
    maintenance = StorageMaintenance(
        database,
        storage_root=tmp_path,
        analytics_writer=writer,
    )
    deleted, vacuumed = await maintenance.delete(("calibration",))
    assert deleted["active_calibration"] == 1
    assert deleted["calibration"] == 1
    assert deleted["projection_files"] >= 4
    assert vacuumed is True
    assert not projection_store.active_pointer_path.exists()
    assert not projection_store.profile_path(profile.profile_id).exists()
    assert not (tmp_path / "baselines" / "default.json").exists()
    assert await migrator.load_active_calibration() is None
    await database.close()


@pytest.mark.asyncio
async def test_corrupt_legacy_projections_are_backed_up_skipped_and_do_not_block(
    tmp_path: Path,
) -> None:
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "session_bad.json").write_bytes(b"{bad-session")
    profiles = tmp_path / "calibration" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "bad.json").write_bytes(b"{bad-profile")
    (tmp_path / "calibration" / "active.json").write_bytes(b"{bad-pointer")
    legacy_store = tmp_path / "store.json"
    legacy_store.write_bytes(b"{bad-store")
    consent = tmp_path / "consent_overrides.json"
    consent.write_bytes(b"{bad-consent")

    database = _database(tmp_path)
    migrator = LegacyDataMigrator(database, storage_root=tmp_path)
    await migrator.migrate_all()
    store = SQLiteKeyValueStore(
        database,
        legacy_store_path=legacy_store,
        legacy_consent_overrides_path=consent,
    )
    assert await store.get_json("consent_ladder_state") is None
    skipped = await database.read(
        lambda connection: [
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT source_kind, diagnostic_code FROM legacy_migrations "
                "WHERE status='skipped' ORDER BY source_kind"
            )
        ]
    )
    assert skipped == [
        ("active_calibration_json", "invalid_active_calibration_pointer"),
        ("calibration_profile_json", "invalid_calibration_profile"),
        ("consent_overrides_json", "invalid_consent_override_json"),
        ("key_value_json", "invalid_key_value_json"),
        ("session_report_json", "invalid_session_report"),
    ]
    assert len(list((database.backup_dir / "legacy").iterdir())) == 5

    second = LegacyDataMigrator(database, storage_root=tmp_path)
    await second.migrate_all()
    assert (
        await database.read(
            lambda connection: connection.execute(
                "SELECT COUNT(*) FROM legacy_migrations"
            ).fetchone()[0]
        )
        == 5
    )
    await database.close()


def hashlib_sha256(value: bytes) -> str:

    return hashlib.sha256(value).hexdigest()
