"""Reversible import of supported pre-SQLite durable file shapes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from cortex.application.clock import SYSTEM_CLOCK, Clock
from cortex.libs.schemas.calibration import ActiveCalibrationPointer, CalibrationProfile
from cortex.libs.schemas.session_report import SessionReport
from cortex.services.capture_service.calibration_store import (
    calibration_profile_sha256,
)
from cortex.storage.database import SQLiteDatabase, StorageCorruptionError
from cortex.storage.intervention_store import _atomic_copy_bytes

_ZERO_BOOT_ID = UUID("00000000-0000-0000-0000-000000000000")
logger = logging.getLogger(__name__)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _unix_ms(value: datetime) -> int:
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return max(0, int(normalized.timestamp() * 1_000))


class LegacyDataMigrator:
    """Import session aggregates, calibration, and legacy AMIP diagnostics.

    The original is never removed. Before an import transaction, exact source
    bytes are copied to ``migration-backups/legacy`` and checksummed. The
    ``legacy_migrations`` row and imported records commit together, making
    every import idempotent and auditable.
    """

    def __init__(
        self,
        database: SQLiteDatabase,
        *,
        storage_root: str | Path,
        clock: Clock | None = None,
        session_retention_days: int = 7,
    ) -> None:
        self._database = database
        self._root = Path(storage_root).expanduser().resolve()
        self._clock = clock or SYSTEM_CLOCK
        self._session_retention_days = max(0, session_retention_days)
        self._lock = asyncio.Lock()
        self._complete = False

    async def migrate_all(self) -> None:
        if self._complete:
            return
        async with self._lock:
            if self._complete:
                return
            await self._database.start()
            await self._migrate_sessions()
            await self._migrate_calibration()
            await self._migrate_policy_logs()
            self._complete = True

    async def _already_imported(self, kind: str, digest: str) -> bool:
        return await self._database.read(
            lambda connection: (
                connection.execute(
                    "SELECT 1 FROM legacy_migrations WHERE source_kind=? AND source_sha256=?",
                    (kind, digest),
                ).fetchone()
                is not None
            )
        )

    async def _backup(
        self,
        path: Path,
        *,
        kind: str,
        digest: str,
    ) -> Path:
        backup = self._database.backup_dir / "legacy" / f"{kind}.{digest}{path.suffix or '.json'}"
        copied = await asyncio.to_thread(_atomic_copy_bytes, path, backup)
        if copied != digest:
            raise StorageCorruptionError(f"legacy {kind} backup checksum mismatch")
        return backup

    async def _record_skipped_source(
        self,
        *,
        path: Path,
        kind: str,
        digest: str,
        backup: Path,
        diagnostic_code: str,
        skipped_records: int = 1,
    ) -> None:
        """Record a backed-up, non-authoritative source we cannot import.

        A malformed historical session, calibration projection, or active
        pointer must never prevent Cortex from opening its authoritative
        database. The exact bytes remain available in the migration backup,
        and the ledger makes the skip deterministic on every later startup.
        """

        def commit(connection: Any) -> None:
            connection.execute(
                "INSERT INTO legacy_migrations("
                "source_kind, source_name, source_sha256, backup_name, "
                "backup_sha256, imported_records, skipped_records, status, "
                "diagnostic_code, imported_at_unix_ms"
                ") VALUES (?, ?, ?, ?, ?, 0, ?, 'skipped', ?, ?) "
                "ON CONFLICT(source_kind, source_sha256) DO NOTHING",
                (
                    kind,
                    path.name,
                    digest,
                    backup.name,
                    digest,
                    max(1, skipped_records),
                    diagnostic_code,
                    self._clock.unix_ms(),
                ),
            )

        await self._database.transaction(commit)
        logger.warning(
            "Skipped backed-up legacy %s source %s (%s)",
            kind,
            path.name,
            diagnostic_code,
        )

    async def _migrate_sessions(self) -> None:
        sessions_dir = self._root / "sessions"
        if not sessions_dir.exists():
            return
        for path in sorted(sessions_dir.glob("session_*.json")):
            if not path.is_file():
                continue
            raw = await asyncio.to_thread(path.read_bytes)
            digest = hashlib.sha256(raw).hexdigest()
            kind = "session_report_json"
            if await self._already_imported(kind, digest):
                continue
            backup = await self._backup(path, kind=kind, digest=digest)
            try:
                report = SessionReport.model_validate_json(raw)
            except ValidationError:
                await self._record_skipped_source(
                    path=path,
                    kind=kind,
                    digest=digest,
                    backup=backup,
                    diagnostic_code="invalid_session_report",
                )
                continue

            def commit(
                connection: Any,
                *,
                kind: str = kind,
                digest: str = digest,
                report: SessionReport = report,
                path: Path = path,
                backup: Path = backup,
            ) -> None:
                if (
                    connection.execute(
                        "SELECT 1 FROM legacy_migrations WHERE source_kind=? AND source_sha256=?",
                        (kind, digest),
                    ).fetchone()
                    is not None
                ):
                    return
                self._upsert_session_sync(connection, report, source_sha256=digest)
                connection.execute(
                    "INSERT INTO legacy_migrations("
                    "source_kind, source_name, source_sha256, backup_name, backup_sha256, "
                    "imported_records, skipped_records, imported_at_unix_ms"
                    ") VALUES (?, ?, ?, ?, ?, 1, 0, ?)",
                    (kind, path.name, digest, backup.name, digest, self._clock.unix_ms()),
                )

            await self._database.transaction(commit)

    async def upsert_session(self, report: SessionReport) -> None:
        """Mirror a current report into the privacy-minimal aggregate table."""

        validated = SessionReport.model_validate(report.model_dump(mode="json"))
        encoded = _canonical_json(validated.model_dump(mode="json"))
        digest = _sha256_text(encoded)
        await self._database.transaction(
            lambda connection: self._upsert_session_sync(
                connection,
                validated,
                source_sha256=digest,
            )
        )

    def _upsert_session_sync(
        self,
        connection: Any,
        report: SessionReport,
        *,
        source_sha256: str,
    ) -> None:
        started = _unix_ms(report.start_time)
        ended = max(started, _unix_ms(report.end_time))
        expires = ended + self._session_retention_days * 86_400_000
        connection.execute(
            "INSERT INTO session_aggregates("
            "session_id, schema_version, started_at_unix_ms, ended_at_unix_ms, "
            "duration_seconds, flow_seconds, hyper_seconds, hypo_seconds, "
            "recovery_seconds, interventions_triggered, interventions_accepted, "
            "breaks_taken, source_sha256, expires_at_unix_ms"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "schema_version=excluded.schema_version, "
            "started_at_unix_ms=excluded.started_at_unix_ms, "
            "ended_at_unix_ms=excluded.ended_at_unix_ms, "
            "duration_seconds=excluded.duration_seconds, "
            "flow_seconds=excluded.flow_seconds, hyper_seconds=excluded.hyper_seconds, "
            "hypo_seconds=excluded.hypo_seconds, recovery_seconds=excluded.recovery_seconds, "
            "interventions_triggered=excluded.interventions_triggered, "
            "interventions_accepted=excluded.interventions_accepted, "
            "breaks_taken=excluded.breaks_taken, source_sha256=excluded.source_sha256, "
            "expires_at_unix_ms=excluded.expires_at_unix_ms",
            (
                report.session_id,
                report.schema_version,
                started,
                ended,
                max(0.0, report.duration_seconds),
                max(0.0, report.time_in_flow_seconds),
                max(0.0, report.time_in_hyper_seconds),
                max(0.0, report.time_in_hypo_seconds),
                max(0.0, report.time_in_recovery_seconds),
                max(0, report.interventions_triggered),
                max(0, report.interventions_accepted),
                max(0, report.breaks_taken),
                source_sha256,
                expires,
            ),
        )

    async def _migrate_calibration(self) -> None:
        calibration_root = self._root / "calibration"
        paths: list[tuple[Path, bool]] = []
        paths.extend(
            (path, False)
            for path in sorted((calibration_root / "profiles").glob("*.json"))
            if path.is_file()
        )
        paths.extend(
            (path, True)
            for path in sorted((calibration_root / "demo_profiles").glob("*.json"))
            if path.is_file()
        )
        for path, _demo in paths:
            raw = await asyncio.to_thread(path.read_bytes)
            digest = hashlib.sha256(raw).hexdigest()
            kind = "calibration_profile_json"
            if await self._already_imported(kind, digest):
                continue
            backup = await self._backup(path, kind=kind, digest=digest)
            try:
                profile = CalibrationProfile.model_validate_json(raw)
            except ValidationError:
                await self._record_skipped_source(
                    path=path,
                    kind=kind,
                    digest=digest,
                    backup=backup,
                    diagnostic_code="invalid_calibration_profile",
                )
                continue
            expected_profile_digest = calibration_profile_sha256(profile)

            def commit_profile(
                connection: Any,
                *,
                kind: str = kind,
                digest: str = digest,
                profile: CalibrationProfile = profile,
                path: Path = path,
                backup: Path = backup,
                expected_profile_digest: str = expected_profile_digest,
            ) -> None:
                if (
                    connection.execute(
                        "SELECT 1 FROM legacy_migrations WHERE source_kind=? AND source_sha256=?",
                        (kind, digest),
                    ).fetchone()
                    is not None
                ):
                    return
                self._upsert_calibration_sync(connection, profile)
                connection.execute(
                    "INSERT INTO legacy_migrations("
                    "source_kind, source_name, source_sha256, backup_name, backup_sha256, "
                    "imported_records, skipped_records, imported_at_unix_ms"
                    ") VALUES (?, ?, ?, ?, ?, 1, 0, ?)",
                    (
                        kind,
                        path.name,
                        digest,
                        backup.name,
                        digest,
                        self._clock.unix_ms(),
                    ),
                )
                stored = connection.execute(
                    "SELECT profile_sha256 FROM calibration_profiles WHERE profile_id=?",
                    (str(profile.profile_id),),
                ).fetchone()
                if stored is None or str(stored[0]) != expected_profile_digest:
                    raise StorageCorruptionError(
                        "calibration profile import checksum verification failed"
                    )

            await self._database.transaction(commit_profile)

        pointer_path = calibration_root / "active.json"
        if not pointer_path.is_file():
            return
        raw = await asyncio.to_thread(pointer_path.read_bytes)
        digest = hashlib.sha256(raw).hexdigest()
        kind = "active_calibration_json"
        if await self._already_imported(kind, digest):
            return
        backup = await self._backup(pointer_path, kind=kind, digest=digest)
        try:
            pointer = ActiveCalibrationPointer.model_validate_json(raw)
        except ValidationError:
            await self._record_skipped_source(
                path=pointer_path,
                kind=kind,
                digest=digest,
                backup=backup,
                diagnostic_code="invalid_active_calibration_pointer",
            )
            return

        def commit_pointer(connection: Any) -> None:
            profile = connection.execute(
                "SELECT profile_sha256, provenance, approved_at_unix_ms "
                "FROM calibration_profiles WHERE profile_id=?",
                (str(pointer.profile_id),),
            ).fetchone()
            if (
                profile is None
                or str(profile[0]) != pointer.profile_sha256
                or str(profile[1]) != "measured"
                or profile[2] is None
            ):
                connection.execute(
                    "INSERT INTO legacy_migrations("
                    "source_kind, source_name, source_sha256, backup_name, "
                    "backup_sha256, imported_records, skipped_records, status, "
                    "diagnostic_code, imported_at_unix_ms"
                    ") VALUES (?, ?, ?, ?, ?, 0, 1, 'skipped', ?, ?) "
                    "ON CONFLICT(source_kind, source_sha256) DO NOTHING",
                    (
                        kind,
                        pointer_path.name,
                        digest,
                        backup.name,
                        digest,
                        "unusable_active_calibration_reference",
                        self._clock.unix_ms(),
                    ),
                )
                return
            connection.execute(
                "INSERT INTO active_calibration("
                "singleton, profile_id, profile_sha256, activated_at_unix_ms"
                ") VALUES (1, ?, ?, ?) ON CONFLICT(singleton) DO UPDATE SET "
                "profile_id=excluded.profile_id, profile_sha256=excluded.profile_sha256, "
                "activated_at_unix_ms=excluded.activated_at_unix_ms",
                (
                    str(pointer.profile_id),
                    pointer.profile_sha256,
                    pointer.activated_at_unix_ms,
                ),
            )
            connection.execute(
                "INSERT INTO legacy_migrations("
                "source_kind, source_name, source_sha256, backup_name, backup_sha256, "
                "imported_records, skipped_records, imported_at_unix_ms"
                ") VALUES (?, ?, ?, ?, ?, 1, 0, ?)",
                (
                    kind,
                    pointer_path.name,
                    digest,
                    backup.name,
                    digest,
                    self._clock.unix_ms(),
                ),
            )

        await self._database.transaction(commit_pointer)

    async def upsert_calibration(
        self,
        profile: CalibrationProfile,
        *,
        active: ActiveCalibrationPointer | None = None,
    ) -> None:
        validated = CalibrationProfile.model_validate(profile.model_dump(mode="json"))

        def commit(connection: Any) -> None:
            self._upsert_calibration_sync(connection, validated)
            if active is not None:
                if active.profile_id != validated.profile_id:
                    raise ValueError("active pointer/profile identity mismatch")
                if active.profile_sha256 != calibration_profile_sha256(validated):
                    raise ValueError("active pointer/profile checksum mismatch")
                connection.execute(
                    "INSERT INTO active_calibration("
                    "singleton, profile_id, profile_sha256, activated_at_unix_ms"
                    ") VALUES (1, ?, ?, ?) ON CONFLICT(singleton) DO UPDATE SET "
                    "profile_id=excluded.profile_id, profile_sha256=excluded.profile_sha256, "
                    "activated_at_unix_ms=excluded.activated_at_unix_ms",
                    (
                        str(active.profile_id),
                        active.profile_sha256,
                        active.activated_at_unix_ms,
                    ),
                )

        await self._database.transaction(commit)

    def upsert_calibration_blocking(
        self,
        profile: CalibrationProfile,
        *,
        active: ActiveCalibrationPointer | None = None,
    ) -> None:
        """Commit calibration from the legacy synchronous activation API."""

        validated = CalibrationProfile.model_validate(profile.model_dump(mode="json"))

        def commit(connection: Any) -> None:
            self._upsert_calibration_sync(connection, validated)
            if active is None:
                return
            if active.profile_id != validated.profile_id:
                raise ValueError("active pointer/profile identity mismatch")
            if active.profile_sha256 != calibration_profile_sha256(validated):
                raise ValueError("active pointer/profile checksum mismatch")
            connection.execute(
                "INSERT INTO active_calibration("
                "singleton, profile_id, profile_sha256, activated_at_unix_ms"
                ") VALUES (1, ?, ?, ?) ON CONFLICT(singleton) DO UPDATE SET "
                "profile_id=excluded.profile_id, profile_sha256=excluded.profile_sha256, "
                "activated_at_unix_ms=excluded.activated_at_unix_ms",
                (
                    str(active.profile_id),
                    active.profile_sha256,
                    active.activated_at_unix_ms,
                ),
            )

        self._database.transaction_sync(commit)

    async def load_active_calibration(self) -> CalibrationProfile | None:
        return await self._database.read(self._load_active_calibration_sync)

    def load_active_calibration_blocking(self) -> CalibrationProfile | None:
        return self._database.call_sync(self._load_active_calibration_sync)

    def load_active_calibration_record_blocking(
        self,
    ) -> tuple[CalibrationProfile, ActiveCalibrationPointer] | None:
        def load(
            connection: Any,
        ) -> tuple[CalibrationProfile, ActiveCalibrationPointer] | None:
            row = connection.execute(
                "SELECT p.profile_json, p.profile_sha256, a.profile_sha256, "
                "a.activated_at_unix_ms FROM active_calibration a "
                "JOIN calibration_profiles p ON p.profile_id=a.profile_id "
                "WHERE a.singleton=1"
            ).fetchone()
            if row is None:
                return None
            profile = CalibrationProfile.model_validate_json(str(row[0]))
            digest = calibration_profile_sha256(profile)
            if digest != str(row[1]) or digest != str(row[2]):
                raise StorageCorruptionError("active calibration database checksum mismatch")
            return profile, ActiveCalibrationPointer(
                profile_id=profile.profile_id,
                profile_sha256=digest,
                activated_at_unix_ms=int(row[3]),
            )

        return self._database.call_sync(load)

    def restore_active_calibration_blocking(
        self,
        prior: tuple[CalibrationProfile, ActiveCalibrationPointer] | None,
    ) -> None:
        def restore(connection: Any) -> None:
            if prior is None:
                connection.execute("DELETE FROM active_calibration WHERE singleton=1")
                return
            profile, pointer = prior
            self._upsert_calibration_sync(connection, profile)
            connection.execute(
                "INSERT INTO active_calibration("
                "singleton, profile_id, profile_sha256, activated_at_unix_ms"
                ") VALUES (1, ?, ?, ?) ON CONFLICT(singleton) DO UPDATE SET "
                "profile_id=excluded.profile_id, profile_sha256=excluded.profile_sha256, "
                "activated_at_unix_ms=excluded.activated_at_unix_ms",
                (
                    str(pointer.profile_id),
                    pointer.profile_sha256,
                    pointer.activated_at_unix_ms,
                ),
            )

        self._database.transaction_sync(restore)

    @staticmethod
    def _load_active_calibration_sync(connection: Any) -> CalibrationProfile | None:
        row = connection.execute(
            "SELECT p.profile_json, p.profile_sha256, a.profile_sha256 "
            "FROM active_calibration a JOIN calibration_profiles p "
            "ON p.profile_id=a.profile_id WHERE a.singleton=1"
        ).fetchone()
        if row is None:
            return None
        profile = CalibrationProfile.model_validate_json(str(row[0]))
        digest = calibration_profile_sha256(profile)
        if digest != str(row[1]) or digest != str(row[2]):
            raise StorageCorruptionError("active calibration database checksum mismatch")
        return profile

    @staticmethod
    def _upsert_calibration_sync(
        connection: Any,
        profile: CalibrationProfile,
    ) -> None:
        encoded = _canonical_json(profile.model_dump(mode="json"))
        digest = calibration_profile_sha256(profile)
        existing = connection.execute(
            "SELECT profile_sha256, profile_json FROM calibration_profiles WHERE profile_id=?",
            (str(profile.profile_id),),
        ).fetchone()
        if existing is not None and (str(existing[0]) != digest or str(existing[1]) != encoded):
            raise StorageCorruptionError(
                f"immutable calibration profile changed: {profile.profile_id}"
            )
        connection.execute(
            "INSERT INTO calibration_profiles("
            "profile_id, profile_sha256, provenance, approved_at_unix_ms, "
            "created_at_unix_ms, profile_json) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(profile_id) DO NOTHING",
            (
                str(profile.profile_id),
                digest,
                str(profile.provenance),
                profile.approved_at_unix_ms,
                profile.created_at_unix_ms,
                encoded,
            ),
        )

    async def _migrate_policy_logs(self) -> None:
        policy_dir = self._root / "policy_log"
        if not policy_dir.exists():
            return
        for path in sorted(policy_dir.glob("*.jsonl")):
            if not path.is_file():
                continue
            raw = await asyncio.to_thread(path.read_bytes)
            digest = hashlib.sha256(raw).hexdigest()
            kind = "legacy_amip_jsonl"
            if await self._already_imported(kind, digest):
                continue
            backup = await self._backup(path, kind=kind, digest=digest)
            decisions: dict[str, dict[str, Any]] = {}
            rewards: dict[str, dict[str, Any]] = {}
            skipped = 0
            lines = raw.decode("utf-8", errors="replace").splitlines()
            for line in lines:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    skipped += 1
                    continue
                if not isinstance(record, dict) or not isinstance(record.get("decision_id"), str):
                    skipped += 1
                    continue
                decision_id = str(record["decision_id"])
                if "probabilities" in record and "action" in record:
                    decisions[decision_id] = record
                if "reward" in record:
                    rewards[decision_id] = record
            state_sha = _sha256_text("legacy-amip-state-unavailable")

            def commit(
                connection: Any,
                *,
                decisions: dict[str, dict[str, Any]] = decisions,
                rewards: dict[str, dict[str, Any]] = rewards,
                state_sha: str = state_sha,
                kind: str = kind,
                path: Path = path,
                digest: str = digest,
                backup: Path = backup,
                skipped: int = skipped,
            ) -> None:
                now = self._clock.unix_ms()
                imported = 0
                for decision_id, record in decisions.items():
                    probabilities = record.get("probabilities", {})
                    action = str(record.get("action", "no_action"))
                    probability = (
                        float(probabilities.get(action, 0.0))
                        if isinstance(probabilities, dict)
                        else 0.0
                    )
                    probability = max(0.0, min(1.0, probability))
                    timestamp = record.get("timestamp", 0.0)
                    occurred = max(
                        0,
                        int(float(timestamp) * 1_000) if isinstance(timestamp, int | float) else 0,
                    )
                    payload = dict(record)
                    payload["legacy_diagnostic_only"] = True
                    encoded = _canonical_json(payload)
                    connection.execute(
                        "INSERT INTO policy_decisions("
                        "decision_id, decision_point_id, policy_name, policy_version, "
                        "policy_state_sha256, selected_arm, selected_probability, "
                        "eligible, available, occurred_at_unix_ms, occurred_at_mono_ns, "
                        "boot_id, intervention_id, payload_json, payload_sha256"
                        ") VALUES (?, ?, 'legacy_amip_diagnostic', 'legacy-jsonl-v1', "
                        "?, ?, ?, 1, 1, ?, 0, ?, NULL, ?, ?) "
                        "ON CONFLICT(decision_id) DO NOTHING",
                        (
                            decision_id,
                            f"legacy:{decision_id}",
                            state_sha,
                            action,
                            probability,
                            occurred,
                            str(_ZERO_BOOT_ID),
                            encoded,
                            _sha256_text(encoded),
                        ),
                    )
                    reward_record = rewards.get(decision_id)
                    if reward_record is not None:
                        reward = max(
                            -1.0,
                            min(1.0, float(reward_record.get("reward", 0.0))),
                        )
                        components = _canonical_json(
                            {"legacy_reward": reward, "diagnostic_only": True}
                        )
                        connection.execute(
                            "INSERT INTO policy_rewards("
                            "decision_id, reward_version, reward_value, "
                            "finalized_at_unix_ms, components_json, payload_sha256"
                            ") VALUES (?, 'legacy-latest-v1', ?, ?, ?, ?) "
                            "ON CONFLICT(decision_id) DO NOTHING",
                            (
                                decision_id,
                                reward,
                                now,
                                components,
                                _sha256_text(components),
                            ),
                        )
                    imported += 1
                connection.execute(
                    "INSERT INTO legacy_migrations("
                    "source_kind, source_name, source_sha256, backup_name, backup_sha256, "
                    "imported_records, skipped_records, imported_at_unix_ms"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        kind,
                        path.name,
                        digest,
                        backup.name,
                        digest,
                        imported,
                        skipped,
                        now,
                    ),
                )

            await self._database.transaction(commit)


__all__ = ["LegacyDataMigrator"]
