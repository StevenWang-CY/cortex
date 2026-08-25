"""Relational persistence adapter for the WP6 intervention aggregate."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from cortex.application.clock import SYSTEM_CLOCK, Clock
from cortex.libs.schemas.intervention_transaction import (
    InterventionTransaction,
    InterventionTransactionJournal,
)
from cortex.storage.database import (
    SQLiteDatabase,
    StorageCorruptionError,
    StorageError,
)


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


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _model_payload(model: Any) -> tuple[str, str]:
    encoded = _canonical_json(model.model_dump(mode="json"))
    return encoded, _sha256_text(encoded)


def _atomic_copy_bytes(source: Path, destination: Path) -> str:
    """Copy exact legacy bytes with fsync + atomic rename; return SHA-256."""

    payload = source.read_bytes()
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temp.chmod(0o600)
        os.replace(temp, destination)
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise
    return _sha256_bytes(payload)


class SQLiteInterventionTransactionStore:
    """Crash-safe SQLite implementation of ``InterventionTransactionStore``.

    Each save commits the validated aggregate JSON and its normalized
    authorization/receipt/lifecycle projections in one ``BEGIN IMMEDIATE``
    transaction.  Reads verify both the aggregate checksum and every
    projection identity, so a partial/manual edit cannot silently grant or
    erase workspace authority.
    """

    def __init__(
        self,
        database: SQLiteDatabase,
        *,
        legacy_json_path: str | Path | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._legacy_json_path = (
            Path(legacy_json_path).expanduser().resolve() if legacy_json_path is not None else None
        )
        self._clock = clock or SYSTEM_CLOCK
        self._legacy_lock = asyncio.Lock()
        self._legacy_checked = False

    @property
    def database(self) -> SQLiteDatabase:
        return self._database

    async def load(self) -> InterventionTransactionJournal:
        await self._database.start()
        await self._ensure_legacy_migrated()

        def load_sync(connection: Any) -> InterventionTransactionJournal:
            transactions: dict[str, InterventionTransaction] = {}
            rows = connection.execute(
                "SELECT intervention_id, aggregate_json, aggregate_sha256 "
                "FROM intervention_transactions ORDER BY created_at_unix_ms, intervention_id"
            ).fetchall()
            for row in rows:
                encoded = str(row["aggregate_json"])
                if _sha256_text(encoded) != str(row["aggregate_sha256"]):
                    raise StorageCorruptionError(
                        f"intervention aggregate checksum mismatch for {row['intervention_id']}"
                    )
                try:
                    transaction = InterventionTransaction.model_validate_json(encoded)
                except ValidationError as exc:
                    raise StorageCorruptionError(
                        "intervention aggregate failed schema validation for "
                        f"{row['intervention_id']}"
                    ) from exc
                canonical, _digest = _model_payload(transaction)
                if canonical != encoded:
                    raise StorageCorruptionError(
                        "intervention aggregate is not canonically encoded for "
                        f"{transaction.intervention_id}"
                    )
                if transaction.intervention_id != str(row["intervention_id"]):
                    raise StorageCorruptionError("intervention aggregate primary key mismatch")
                self._verify_projection_sync(connection, transaction)
                transactions[transaction.intervention_id] = transaction
            return InterventionTransactionJournal(transactions=transactions)

        journal = await self._database.read(load_sync)
        return journal.model_copy(deep=True)

    async def save(self, journal: InterventionTransactionJournal) -> None:
        validated = InterventionTransactionJournal.model_validate(journal.model_dump(mode="json"))
        snapshot = validated.model_copy(deep=True)

        def save_sync(connection: Any) -> None:
            self._replace_journal_sync(connection, snapshot)

        await self._database.transaction(save_sync)

    @classmethod
    def _replace_journal_sync(
        cls,
        connection: Any,
        journal: InterventionTransactionJournal,
    ) -> None:
        desired_ids = set(journal.transactions)
        existing_rows = connection.execute(
            "SELECT intervention_id, aggregate_sha256 FROM intervention_transactions"
        ).fetchall()
        existing = {
            str(row["intervention_id"]): str(row["aggregate_sha256"]) for row in existing_rows
        }
        for stale_id in set(existing) - desired_ids:
            connection.execute(
                "DELETE FROM intervention_transactions WHERE intervention_id=?",
                (stale_id,),
            )
        for transaction in journal.transactions.values():
            aggregate_json, aggregate_sha = _model_payload(transaction)
            if existing.get(transaction.intervention_id) == aggregate_sha:
                # A matching content digest makes this retry a true no-op.
                continue
            connection.execute(
                "INSERT INTO intervention_transactions("
                "intervention_id, manifest_sha256, lifecycle_state, revision, "
                "created_at_unix_ms, updated_at_unix_ms, aggregate_json, aggregate_sha256"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(intervention_id) DO UPDATE SET "
                "manifest_sha256=excluded.manifest_sha256, "
                "lifecycle_state=excluded.lifecycle_state, revision=excluded.revision, "
                "created_at_unix_ms=excluded.created_at_unix_ms, "
                "updated_at_unix_ms=excluded.updated_at_unix_ms, "
                "aggregate_json=excluded.aggregate_json, "
                "aggregate_sha256=excluded.aggregate_sha256",
                (
                    transaction.intervention_id,
                    transaction.manifest.manifest_sha256,
                    str(transaction.state),
                    transaction.revision,
                    transaction.created_at_unix_ms,
                    transaction.updated_at_unix_ms,
                    aggregate_json,
                    aggregate_sha,
                ),
            )
            cls._replace_projection_sync(connection, transaction)

    @classmethod
    def _replace_projection_sync(
        cls,
        connection: Any,
        transaction: InterventionTransaction,
    ) -> None:
        intervention_id = transaction.intervention_id
        # Child tables are projections of one already-validated aggregate.
        # Replacing them inside the same transaction is simpler and safer than
        # trying to infer fine-grained diffs across mutable ledger entries.
        for table in (
            "intervention_consent_evidence",
            "intervention_receipts",
            "intervention_dispatch_bindings",
            "intervention_restores",
            "intervention_transitions",
            "intervention_authorizations",
        ):
            connection.execute(
                f"DELETE FROM {table} WHERE intervention_id=?",  # noqa: S608 - closed catalog
                (intervention_id,),
            )

        for entry in transaction.authorizations:
            payload, digest = _model_payload(entry)
            authorization = entry.authorization
            connection.execute(
                "INSERT INTO intervention_authorizations("
                "authorization_id, intervention_id, authorization_request_id, state, "
                "issued_at_unix_ms, expires_at_unix_ms, consumed_at_unix_ms, "
                "payload_json, payload_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    authorization.authorization_id,
                    intervention_id,
                    authorization.authorization_request_id,
                    str(entry.state),
                    authorization.issued_at_unix_ms,
                    authorization.expires_at_unix_ms,
                    entry.consumed_at_unix_ms,
                    payload,
                    digest,
                ),
            )
        for receipt in transaction.receipts:
            payload, digest = _model_payload(receipt)
            connection.execute(
                "INSERT INTO intervention_receipts("
                "receipt_id, intervention_id, command_id, action_id, phase, attempt, "
                "idempotency_key, status, verification, ended_at_unix_ms, payload_json, "
                "payload_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    receipt.receipt_id,
                    intervention_id,
                    receipt.authorization_id,
                    receipt.action_id,
                    str(receipt.phase),
                    receipt.attempt,
                    receipt.idempotency_key,
                    str(receipt.status),
                    str(receipt.verification),
                    receipt.ended_at_unix_ms,
                    payload,
                    digest,
                ),
            )
        for ordinal, transition in enumerate(transaction.transitions):
            payload, digest = _model_payload(transition)
            connection.execute(
                "INSERT INTO intervention_transitions("
                "intervention_id, ordinal, from_state, to_state, reason, at_unix_ms, "
                "payload_json, payload_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    intervention_id,
                    ordinal,
                    str(transition.from_state) if transition.from_state is not None else None,
                    str(transition.to_state),
                    transition.reason,
                    transition.at_unix_ms,
                    payload,
                    digest,
                ),
            )
        for binding in transaction.dispatch_history:
            payload, digest = _model_payload(binding)
            connection.execute(
                "INSERT INTO intervention_dispatch_bindings("
                "command_id, intervention_id, bound_at_unix_ms, payload_json, payload_sha256"
                ") VALUES (?, ?, ?, ?, ?)",
                (
                    binding.command_id,
                    intervention_id,
                    binding.bound_at_unix_ms,
                    payload,
                    digest,
                ),
            )
        active_restore_id = (
            transaction.active_restore.restore_id
            if transaction.active_restore is not None
            else None
        )
        for restore in transaction.restore_history:
            payload, digest = _model_payload(restore)
            connection.execute(
                "INSERT INTO intervention_restores("
                "restore_id, intervention_id, reason, requested_at_unix_ms, is_active, "
                "payload_json, payload_sha256) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    restore.restore_id,
                    intervention_id,
                    str(restore.reason),
                    restore.requested_at_unix_ms,
                    int(restore.restore_id == active_restore_id),
                    payload,
                    digest,
                ),
            )
        for receipt_id in transaction.consent_evidence_receipt_ids:
            connection.execute(
                "INSERT INTO intervention_consent_evidence(intervention_id, receipt_id) "
                "VALUES (?, ?)",
                (intervention_id, receipt_id),
            )

    @staticmethod
    def _projection_map(
        connection: Any,
        *,
        table: str,
        id_column: str,
        intervention_id: str,
    ) -> dict[str, str]:
        rows = connection.execute(
            f"SELECT {id_column}, payload_sha256 FROM {table} "  # noqa: S608 - closed catalog
            "WHERE intervention_id=?",
            (intervention_id,),
        ).fetchall()
        return {str(row[0]): str(row[1]) for row in rows}

    @classmethod
    def _verify_projection_sync(
        cls,
        connection: Any,
        transaction: InterventionTransaction,
    ) -> None:
        intervention_id = transaction.intervention_id
        expected_authorizations = {
            entry.authorization.authorization_id: _model_payload(entry)[1]
            for entry in transaction.authorizations
        }
        expected_receipts = {
            receipt.receipt_id: _model_payload(receipt)[1] for receipt in transaction.receipts
        }
        expected_dispatches = {
            binding.command_id: _model_payload(binding)[1]
            for binding in transaction.dispatch_history
        }
        expected_restores = {
            restore.restore_id: _model_payload(restore)[1]
            for restore in transaction.restore_history
        }
        actual_authorizations = cls._projection_map(
            connection,
            table="intervention_authorizations",
            id_column="authorization_id",
            intervention_id=intervention_id,
        )
        actual_receipts = cls._projection_map(
            connection,
            table="intervention_receipts",
            id_column="receipt_id",
            intervention_id=intervention_id,
        )
        actual_dispatches = cls._projection_map(
            connection,
            table="intervention_dispatch_bindings",
            id_column="command_id",
            intervention_id=intervention_id,
        )
        actual_restores = cls._projection_map(
            connection,
            table="intervention_restores",
            id_column="restore_id",
            intervention_id=intervention_id,
        )
        transition_rows = connection.execute(
            "SELECT ordinal, payload_sha256 FROM intervention_transitions "
            "WHERE intervention_id=? ORDER BY ordinal",
            (intervention_id,),
        ).fetchall()
        actual_transitions = {
            int(row["ordinal"]): str(row["payload_sha256"]) for row in transition_rows
        }
        expected_transitions = {
            ordinal: _model_payload(transition)[1]
            for ordinal, transition in enumerate(transaction.transitions)
        }
        evidence_rows = connection.execute(
            "SELECT receipt_id FROM intervention_consent_evidence "
            "WHERE intervention_id=? ORDER BY receipt_id",
            (intervention_id,),
        ).fetchall()
        actual_evidence = sorted(str(row[0]) for row in evidence_rows)
        expected_evidence = sorted(transaction.consent_evidence_receipt_ids)
        if any(
            (
                actual_authorizations != expected_authorizations,
                actual_receipts != expected_receipts,
                actual_dispatches != expected_dispatches,
                actual_restores != expected_restores,
                actual_transitions != expected_transitions,
                actual_evidence != expected_evidence,
            )
        ):
            raise StorageCorruptionError(
                f"intervention relational projection mismatch for {intervention_id}"
            )

    async def _ensure_legacy_migrated(self) -> None:
        if self._legacy_checked:
            return
        async with self._legacy_lock:
            if self._legacy_checked:
                return
            path = self._legacy_json_path
            if path is None or not path.exists():
                self._legacy_checked = True
                return
            raw = await asyncio.to_thread(path.read_bytes)
            source_sha = _sha256_bytes(raw)

            def already_imported(connection: Any) -> bool:
                row = connection.execute(
                    "SELECT 1 FROM legacy_migrations "
                    "WHERE source_kind='intervention_transactions_json' "
                    "AND source_sha256=?",
                    (source_sha,),
                ).fetchone()
                return row is not None

            if await self._database.read(already_imported):
                self._legacy_checked = True
                return
            backup_path = (
                self._database.backup_dir
                / "legacy"
                / f"intervention_transactions.{source_sha}.json"
            )
            backup_sha = await asyncio.to_thread(
                _atomic_copy_bytes,
                path,
                backup_path,
            )
            if backup_sha != source_sha:
                raise StorageCorruptionError("legacy intervention backup checksum mismatch")
            try:
                legacy = InterventionTransactionJournal.model_validate_json(raw)
            except ValidationError as exc:
                raise StorageCorruptionError(
                    "legacy intervention journal is invalid; the original and "
                    f"verified backup were retained ({backup_path.name})"
                ) from exc

            def import_sync(connection: Any) -> None:
                if (
                    connection.execute(
                        "SELECT 1 FROM legacy_migrations "
                        "WHERE source_kind='intervention_transactions_json' "
                        "AND source_sha256=?",
                        (source_sha,),
                    ).fetchone()
                    is not None
                ):
                    return
                existing_rows = connection.execute(
                    "SELECT intervention_id, aggregate_json FROM intervention_transactions"
                ).fetchall()
                merged = legacy.model_copy(deep=True)
                for row in existing_rows:
                    existing_tx = InterventionTransaction.model_validate_json(
                        str(row["aggregate_json"])
                    )
                    legacy_tx = merged.transactions.get(existing_tx.intervention_id)
                    if (
                        legacy_tx is not None
                        and _model_payload(legacy_tx)[1] != _model_payload(existing_tx)[1]
                    ):
                        raise StorageError(
                            "legacy intervention migration conflicts with an existing "
                            f"transaction: {existing_tx.intervention_id}"
                        )
                    merged.transactions[existing_tx.intervention_id] = existing_tx
                self._replace_journal_sync(connection, merged)
                connection.execute(
                    "INSERT INTO legacy_migrations("
                    "source_kind, source_name, source_sha256, backup_name, backup_sha256, "
                    "imported_records, skipped_records, imported_at_unix_ms"
                    ") VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
                    (
                        "intervention_transactions_json",
                        path.name,
                        source_sha,
                        backup_path.name,
                        backup_sha,
                        len(legacy.transactions),
                        self._clock.unix_ms(),
                    ),
                )

            await self._database.transaction(import_sync)
            self._legacy_checked = True


__all__ = ["SQLiteInterventionTransactionStore"]
