"""User-visible export, deletion, retention, and health operations."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from cortex.application.clock import SYSTEM_CLOCK, Clock
from cortex.libs.schemas.storage import (
    StorageDeleteScope,
    StorageExportCategory,
    StorageExportResponse,
    StorageHealthReport,
)
from cortex.libs.utils.atomic_write import atomic_write_json, atomic_write_text
from cortex.storage.database import DEFAULT_SESSION_RETENTION_DAYS, SQLiteDatabase
from cortex.storage.event_writer import BoundedAnalyticsWriter

# D13: decisions recorded under the randomized research policy are the
# study's primary data (propensities, randomization ids, consent version)
# and must outlive the operational ``error_retention_days`` window that
# prunes ordinary policy rows; the user's explicit ``delete`` still erases
# them. Cascades from ``policy_decisions`` take deliveries, outcomes,
# rewards, outcome windows and observations with them, which is why the
# exemption lives on the parent row.
RESEARCH_POLICY_MODE = "research_randomized"

_MUTATION_RISK_STATES = {
    "applying",
    "applied",
    "partial",
    "restoring",
    "restore_failed",
}


class ActiveInterventionDataError(RuntimeError):
    """Deletion would erase evidence needed to restore a workspace effect."""


class StorageMaintenance:
    def __init__(
        self,
        database: SQLiteDatabase,
        *,
        storage_root: str | Path,
        analytics_writer: BoundedAnalyticsWriter,
        clock: Clock | None = None,
        retention_days: dict[str, int] | None = None,
        namespace: str = "cortex",
        legacy_store_path: str | Path | None = None,
    ) -> None:
        self._database = database
        self._root = Path(storage_root).expanduser().resolve()
        self._exports_dir = self._root / "exports"
        self._writer = analytics_writer
        self._clock = clock or SYSTEM_CLOCK
        self.retention_days = dict(retention_days or {})
        self._namespace = namespace
        self._legacy_store_path = (
            Path(legacy_store_path).expanduser().resolve()
            if legacy_store_path is not None
            else None
        )
        self._last_health_report: StorageHealthReport | None = None

    @property
    def last_health_report(self) -> StorageHealthReport | None:
        """Most recent :meth:`health` result, or ``None`` before the first probe.

        D4: the unauthenticated, unlimited ``GET /health`` must not run a
        ``PRAGMA quick_check`` (O(database) on the single DB worker) per
        call. It reports this cached snapshot instead; the live probe
        stays on the authenticated ``GET /storage/status``.
        """
        return self._last_health_report

    async def health(self, *, full_integrity_check: bool = False) -> StorageHealthReport:
        raw = await self._database.health(full_integrity_check=full_integrity_check)
        last_error = raw.pop("last_error", None)
        raw["error_code"] = "storage_unavailable" if last_error else None
        # ``health`` itself is the currently pending read operation; expose
        # only work queued behind/in addition to that probe.
        raw["pending_operations"] = max(0, int(raw["pending_operations"]) - 1)
        raw["analytics_queue_depth"] = self._writer.queue_depth
        raw["analytics_dropped_total"] = self._writer.dropped_total
        report = StorageHealthReport.model_validate(raw)
        self._last_health_report = report
        return report

    async def export(
        self,
        categories: tuple[StorageExportCategory, ...],
    ) -> StorageExportResponse:
        """Write a deterministic, user-owned JSON export under storage root."""

        selected = set(categories)

        def collect(connection: Any) -> tuple[dict[str, Any], dict[str, int]]:
            data: dict[str, Any] = {}
            counts: dict[str, int] = {}
            if "consent" in selected:
                row = connection.execute(
                    "SELECT value_json FROM key_values "
                    "WHERE namespace=? AND key='consent_ladder_state'",
                    (self._namespace,),
                ).fetchone()
                records = [] if row is None else [json.loads(str(row[0]))]
                data["consent"] = records
                counts["consent"] = len(records)
            if "interventions" in selected:
                records = [
                    json.loads(str(row[0]))
                    for row in connection.execute(
                        "SELECT aggregate_json FROM intervention_transactions "
                        "ORDER BY created_at_unix_ms, intervention_id"
                    )
                ]
                data["interventions"] = records
                counts["interventions"] = len(records)
            if "policy" in selected:
                decisions: list[dict[str, Any]] = []
                for row in connection.execute(
                    "SELECT d.payload_json, l.payload_json, o.payload_json, "
                    "r.reward_version, r.reward_value, r.components_json "
                    "FROM policy_decisions d "
                    "LEFT JOIN policy_deliveries l ON l.decision_id=d.decision_id "
                    "LEFT JOIN policy_outcomes o ON o.decision_id=d.decision_id "
                    "LEFT JOIN policy_rewards r ON r.decision_id=d.decision_id "
                    "AND r.reward_version=d.reward_version "
                    "ORDER BY d.occurred_at_unix_ms, d.decision_id"
                ):
                    decisions.append(
                        {
                            "decision": json.loads(str(row[0])),
                            "delivery": json.loads(str(row[1])) if row[1] else None,
                            "outcome": json.loads(str(row[2])) if row[2] else None,
                            "reward": (
                                {
                                    "version": str(row[3]),
                                    "value": float(row[4]),
                                    "components": json.loads(str(row[5])),
                                }
                                if row[3] is not None
                                else None
                            ),
                        }
                    )
                data["policy"] = decisions
                counts["policy"] = len(decisions)
            if "calibration" in selected:
                records = [
                    json.loads(str(row[0]))
                    for row in connection.execute(
                        "SELECT profile_json FROM calibration_profiles "
                        "ORDER BY created_at_unix_ms, profile_id"
                    )
                ]
                active = connection.execute(
                    "SELECT profile_id, profile_sha256, activated_at_unix_ms "
                    "FROM active_calibration WHERE singleton=1"
                ).fetchone()
                data["calibration"] = {
                    "profiles": records,
                    "active": dict(active) if active is not None else None,
                }
                counts["calibration"] = len(records)
            if "sessions" in selected:
                records = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT session_id, schema_version, started_at_unix_ms, "
                        "ended_at_unix_ms, duration_seconds, flow_seconds, hyper_seconds, "
                        "hypo_seconds, recovery_seconds, interventions_triggered, "
                        "interventions_accepted, breaks_taken, source_sha256 "
                        "FROM session_aggregates ORDER BY started_at_unix_ms, session_id"
                    )
                ]
                data["sessions"] = records
                counts["sessions"] = len(records)
            if "derived" in selected:
                records = []
                for row in connection.execute(
                    "SELECT key, value_kind, value_json, expires_at_unix_ms "
                    "FROM key_values WHERE namespace=? "
                    "AND key <> 'consent_ladder_state' ORDER BY key",
                    (self._namespace,),
                ):
                    records.append(
                        {
                            "key": str(row[0]),
                            "kind": str(row[1]),
                            "value": json.loads(str(row[2])),
                            "expires_at_unix_ms": row[3],
                        }
                    )
                data["derived"] = records
                counts["derived"] = len(records)
            return data, counts

        data, counts = await self._database.read(collect)
        export_id = uuid4()
        document = {
            "schema_version": "1.0",
            "export_id": str(export_id),
            "generated_at_unix_ms": self._clock.unix_ms(),
            "categories": list(categories),
            "record_counts": counts,
            "data": data,
            "notice": (
                "Cortex exports contain derived local activity and may include "
                "workspace targets from intervention receipts. No API keys or "
                "capability tokens are stored in the database."
            ),
        }
        encoded = json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        filename = f"cortex-export-{export_id}.json"
        destination = self._exports_dir / filename
        await asyncio.to_thread(
            atomic_write_text,
            destination,
            encoded,
        )
        try:
            destination.chmod(0o600)
        except OSError:
            pass
        payload = await asyncio.to_thread(destination.read_bytes)
        return StorageExportResponse.from_clock(
            self._clock,
            export_id=export_id,
            filename=filename,
            sha256=hashlib.sha256(payload).hexdigest(),
            bytes_written=len(payload),
            record_counts=counts,
        )

    async def delete(
        self,
        scopes: tuple[StorageDeleteScope, ...],
    ) -> tuple[dict[str, int], bool]:
        selected: set[str] = (
            {
                "consent",
                "interventions",
                "policy",
                "calibration",
                "sessions",
                "derived",
                "analytics",
            }
            if "all" in scopes
            else set(scopes)
        )

        legacy_kinds: set[str] = set()
        if "sessions" in selected:
            legacy_kinds.add("session_report_json")
        if "calibration" in selected:
            legacy_kinds.update({"calibration_profile_json", "active_calibration_json"})
        if "policy" in selected:
            legacy_kinds.add("legacy_amip_jsonl")
        if "interventions" in selected:
            legacy_kinds.add("intervention_transactions_json")
        if selected.intersection({"consent", "derived"}):
            legacy_kinds.add("key_value_json")
        if "consent" in selected:
            legacy_kinds.add("consent_overrides_json")

        def erase(connection: Any) -> tuple[dict[str, int], tuple[str, ...]]:
            if "interventions" in selected:
                risky = connection.execute(
                    "SELECT intervention_id, lifecycle_state "
                    "FROM intervention_transactions WHERE lifecycle_state IN "
                    "('applying', 'applied', 'partial', 'restoring', 'restore_failed') "
                    "LIMIT 1"
                ).fetchone()
                if risky is not None:
                    raise ActiveInterventionDataError(
                        "restore or emergency-restore active Cortex effects before "
                        f"deleting intervention evidence ({risky['intervention_id']})"
                    )
            deleted: dict[str, int] = {}

            def execute(label: str, sql: str, parameters: tuple[Any, ...] = ()) -> None:
                cursor = connection.execute(sql, parameters)
                deleted[label] = deleted.get(label, 0) + max(0, int(cursor.rowcount))

            if "analytics" in selected:
                execute("analytics", "DELETE FROM analytics_events")
            if "sessions" in selected:
                execute("sessions", "DELETE FROM session_aggregates")
            if "calibration" in selected:
                execute("active_calibration", "DELETE FROM active_calibration")
                execute("calibration", "DELETE FROM calibration_profiles")
            if "policy" in selected:
                # Child decision rows cascade; policy state is independent.
                execute("policy", "DELETE FROM policy_decisions")
                execute("policy_states", "DELETE FROM policy_states")
                execute(
                    "derived",
                    "DELETE FROM key_values WHERE namespace=? AND key='bandit_weights'",
                    (self._namespace,),
                )
            if "interventions" in selected:
                execute("interventions", "DELETE FROM intervention_transactions")
            if "consent" in selected:
                execute(
                    "consent",
                    "DELETE FROM key_values WHERE namespace=? AND key='consent_ladder_state'",
                    (self._namespace,),
                )
            if "derived" in selected:
                execute(
                    "derived",
                    "DELETE FROM key_values WHERE namespace=? AND key <> 'consent_ladder_state'",
                    (self._namespace,),
                )
            backup_names: tuple[str, ...] = ()
            if legacy_kinds:
                placeholders = ",".join("?" for _ in legacy_kinds)
                backup_names = tuple(
                    str(row[0])
                    for row in connection.execute(
                        "SELECT backup_name FROM legacy_migrations "
                        f"WHERE source_kind IN ({placeholders})",
                        tuple(sorted(legacy_kinds)),
                    )
                )
            return deleted, backup_names

        deleted, backup_names = await self._database.transaction(erase)
        projection_count = await asyncio.to_thread(
            self._delete_compatibility_projections,
            selected,
            backup_names,
        )
        if projection_count:
            deleted["projection_files"] = projection_count
        if legacy_kinds:
            placeholders = ",".join("?" for _ in legacy_kinds)
            parameters = tuple(sorted(legacy_kinds))

            def delete_ledger(connection: Any) -> int:
                return max(
                    0,
                    int(
                        connection.execute(
                            f"DELETE FROM legacy_migrations WHERE source_kind IN ({placeholders})",
                            parameters,
                        ).rowcount
                    ),
                )

            ledger_deleted = await self._database.transaction(delete_ledger)
            if ledger_deleted:
                deleted["migration_ledger"] = ledger_deleted
        vacuumed = False
        if any(deleted.values()):
            await self._database.maintenance(lambda connection: connection.execute("VACUUM"))
            vacuumed = True
        return deleted, vacuumed

    def _delete_compatibility_projections(
        self,
        selected: set[str],
        backup_names: tuple[str, ...],
    ) -> int:
        """Remove exact pre-SQLite projections and their migration copies.

        The migration ledger remains in SQLite until this cleanup succeeds.
        A retry therefore cannot accidentally re-import a surviving source.
        """

        candidates: set[Path] = set()
        if "sessions" in selected:
            for pattern in ("session_*.json", "session_*.jsonl", "session_*.md"):
                candidates.update((self._root / "sessions").glob(pattern))
        if "calibration" in selected:
            candidates.add(self._root / "calibration" / "active.json")
            candidates.add(self._root / "baselines" / "default.json")
            candidates.update((self._root / "calibration" / "profiles").glob("*.json"))
            candidates.update((self._root / "calibration" / "demo_profiles").glob("*.json"))
            candidates.update((self._root / "baselines").glob("baseline_*.json"))
        if "policy" in selected:
            candidates.update((self._root / "policy_log").glob("*.jsonl"))
        if "interventions" in selected:
            candidates.add(self._root / "intervention_transactions.json")
        if "consent" in selected:
            candidates.add(self._root / "consent_overrides.json")
        for backup_name in backup_names:
            if Path(backup_name).name != backup_name:
                raise ValueError("invalid migration backup name")
            candidates.add(self._database.backup_dir / "legacy" / backup_name)

        removed = 0
        for path in sorted(candidates, key=str):
            # unlink() on a symlink removes the link itself, never its target.
            if path.is_symlink() or path.is_file():
                path.unlink()
                removed += 1

        if self._legacy_store_path is not None and selected.intersection({"consent", "derived"}):
            removed += self._sanitize_legacy_store(selected)
        return removed

    def _sanitize_legacy_store(self, selected: set[str]) -> int:
        path = self._legacy_store_path
        if path is None or (not path.is_file() and not path.is_symlink()):
            return 0
        if path.is_symlink() or selected.issuperset({"consent", "derived"}):
            path.unlink()
            return 1
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            # An unreadable obsolete file cannot prove the requested class is
            # absent, so erasing the entire projection is the safe result.
            path.unlink()
            return 1
        data = document.get("data") if isinstance(document, dict) else None
        expiry = document.get("expiry") if isinstance(document, dict) else None
        if not isinstance(data, dict) or not isinstance(expiry, dict):
            path.unlink()
            return 1
        prefix = f"{self._namespace}:"
        for internal_key in tuple(data):
            if not isinstance(internal_key, str) or not internal_key.startswith(prefix):
                continue
            key = internal_key[len(prefix) :]
            remove = ("consent" in selected and key == "consent_ladder_state") or (
                "derived" in selected and key != "consent_ladder_state"
            )
            if remove:
                data.pop(internal_key, None)
                expiry.pop(internal_key, None)
        atomic_write_json(path, document)
        return 1

    async def enforce_retention(self) -> dict[str, int]:
        """Delete expired non-authority records; active effects are immortal."""

        now = self._clock.unix_ms()
        session_days = max(
            0,
            int(self.retention_days.get("sessions", DEFAULT_SESSION_RETENTION_DAYS)),
        )
        policy_days = max(0, int(self.retention_days.get("policy", 90)))
        intervention_days = max(
            0,
            int(self.retention_days.get("interventions", 90)),
        )

        def sweep(connection: Any) -> dict[str, int]:
            deleted: dict[str, int] = {}
            commands = {
                "key_values": (
                    "DELETE FROM key_values WHERE expires_at_unix_ms IS NOT NULL "
                    "AND expires_at_unix_ms <= ?",
                    (now,),
                ),
                "analytics": (
                    "DELETE FROM analytics_events WHERE expires_at_unix_ms <= ?",
                    (now,),
                ),
                "sessions": (
                    "DELETE FROM session_aggregates WHERE ended_at_unix_ms <= ?",
                    (now - session_days * 86_400_000,),
                ),
                "policy": (
                    "DELETE FROM policy_decisions WHERE occurred_at_unix_ms <= ? "
                    "AND policy_mode <> ?",
                    (now - policy_days * 86_400_000, RESEARCH_POLICY_MODE),
                ),
                "interventions": (
                    "DELETE FROM intervention_transactions "
                    "WHERE lifecycle_state IN ('restored', 'abandoned', 'failed') "
                    "AND updated_at_unix_ms <= ?",
                    (now - intervention_days * 86_400_000,),
                ),
            }
            for label, (sql, parameters) in commands.items():
                cursor = connection.execute(sql, parameters)
                deleted[label] = max(0, int(cursor.rowcount))
            return deleted

        return await self._database.transaction(sweep)


__all__ = ["RESEARCH_POLICY_MODE", "ActiveInterventionDataError", "StorageMaintenance"]
