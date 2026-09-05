"""Single-connection, rollback-journal SQLite runtime.

SQLite calls can include a FULL-synchronous fsync and therefore must never run
on Cortex's capture/event loop.  :class:`SQLiteDatabase` owns one dedicated
worker thread and creates the connection on that thread.  Every read, critical
transaction, backup, and maintenance operation is serialized through that
same worker.  Best-effort analytics use the bounded writer in
``cortex.storage.event_writer`` rather than submitting unbounded executor work.

The deployment intentionally uses the rollback journal.  WAL is not enabled
until the packaged SQLite build and a multi-process stress matrix are fixed and
validated.  This also keeps the durability model aligned with SQLite's atomic
commit and hot-journal recovery protocol.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import logging
import os
import sqlite3
import stat
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from importlib import resources
from pathlib import Path
from typing import Any, TypeVar, cast
from uuid import uuid4

from cortex.application.clock import SYSTEM_CLOCK, Clock

logger = logging.getLogger(__name__)

T = TypeVar("T")

# ``STRICT`` tables are the newest SQLite feature used by the schema.  They
# were introduced in SQLite 3.37.0, so accepting an older runtime would make
# migration behavior platform-dependent.
MINIMUM_SQLITE_VERSION = (3, 37, 0)
CURRENT_SCHEMA_VERSION = 2
CORTEX_APPLICATION_ID = 0x43545831  # ASCII-ish "CTX1", positive signed int.
# D6: in-code fallback for session aggregate/report retention when the
# caller does not thread ``StorageConfig.session_retention_days`` through.
# Sessions feed the History tab and trends; the size budget
# (``max_total_size_mb``) bounds disk growth, so time-based expiry can be
# generous. Keep in sync with the ``settings.py`` default (patch note).
DEFAULT_SESSION_RETENTION_DAYS = 180


class StorageError(RuntimeError):
    """Base class for durable local-storage failures."""


class StorageCompatibilityError(StorageError):
    """The SQLite runtime or on-disk schema is unsupported."""


class StorageCorruptionError(StorageError):
    """The database failed an integrity or checksum invariant."""


class StorageCapacityError(StorageError):
    """The filesystem or SQLite database is out of space."""


class StorageReadOnlyError(StorageError):
    """A required durable mutation could not be written."""


class StorageBusyError(StorageError):
    """The database remained locked beyond the configured busy timeout."""


def _version_tuple(raw: str) -> tuple[int, int, int]:
    parts = raw.split(".")
    try:
        padded = [int(part) for part in parts[:3]]
    except ValueError as exc:  # pragma: no cover - sqlite owns the string
        raise StorageCompatibilityError(
            f"SQLite returned an invalid version string: {raw!r}"
        ) from exc
    while len(padded) < 3:
        padded.append(0)
    return cast("tuple[int, int, int]", tuple(padded))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sqlite_error(exc: BaseException, *, operation: str) -> StorageError:
    """Map stable SQLite result families to actionable storage failures."""

    code = getattr(exc, "sqlite_errorcode", None)
    primary = int(code) & 0xFF if isinstance(code, int) else None
    message = f"SQLite {operation} failed: {exc}"
    if primary in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}:
        return StorageCorruptionError(message)
    if primary == sqlite3.SQLITE_FULL or (
        isinstance(exc, OSError) and exc.errno in {errno.ENOSPC, errno.EDQUOT}
    ):
        return StorageCapacityError(message)
    if primary in {sqlite3.SQLITE_READONLY, sqlite3.SQLITE_PERM}:
        return StorageReadOnlyError(message)
    if primary in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
        return StorageBusyError(message)
    return StorageError(message)


class SQLiteDatabase:
    """Own exactly one SQLite connection on one dedicated worker thread.

    The class is intentionally a small execution boundary rather than an ORM.
    Repositories supply typed callbacks, and this owner supplies ordering,
    transactions, durability settings, migration, health, and lifecycle.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Clock | None = None,
        busy_timeout_ms: int = 5_000,
        backup_retention_count: int = 3,
        minimum_sqlite_version: tuple[int, int, int] = MINIMUM_SQLITE_VERSION,
    ) -> None:
        if busy_timeout_ms < 1 or busy_timeout_ms > 60_000:
            raise ValueError("busy_timeout_ms must be in 1..60000")
        if backup_retention_count < 1 or backup_retention_count > 20:
            raise ValueError("backup_retention_count must be in 1..20")
        requested_path = Path(path).expanduser()
        self._requested_path = requested_path.absolute()
        self.path = requested_path.resolve()
        self.backup_dir = self.path.parent / "migration-backups"
        self._clock = clock or SYSTEM_CLOCK
        self._busy_timeout_ms = busy_timeout_ms
        self._backup_retention_count = backup_retention_count
        self._minimum_sqlite_version = minimum_sqlite_version
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="cortex-sqlite-writer",
        )
        self._connection: sqlite3.Connection | None = None
        self._worker_thread_id: int | None = None
        self._start_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._started = False
        self._closed = False
        self._pending_lock = threading.Lock()
        self._pending_operations = 0
        self._last_error: str | None = None
        self._last_integrity_check_unix_ms: int | None = None

    @property
    def started(self) -> bool:
        return self._started

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def pending_operations(self) -> int:
        with self._pending_lock:
            return self._pending_operations

    @property
    def last_error(self) -> str | None:
        return self._last_error

    async def start(self) -> None:
        """Open, verify, configure, and migrate the database exactly once."""

        if self._started:
            return
        if self._closed:
            raise StorageError("cannot restart a closed SQLiteDatabase")
        async with self._start_lock:
            if self._started:
                return
            await self._submit_raw(self._open_sync, critical=True)
            self._started = True

    def start_sync(self) -> None:
        """Open synchronously for a legacy command invoked before ``start``.

        The connection is still created on the same dedicated owner thread;
        this method does not create a second connection.
        """

        if self._started:
            return
        if self._closed:
            raise StorageError("cannot restart a closed SQLiteDatabase")
        future = self._executor.submit(self._open_sync)
        future.result(timeout=max(10.0, self._busy_timeout_ms / 1_000 + 5.0))
        self._started = True

    def _prepare_directory_sync(self) -> None:
        if self._requested_path.is_symlink():
            raise StorageCompatibilityError("SQLite database path must not be a symbolic link")
        broad_roots = {
            Path(self.path.anchor).resolve(),
            Path.home().resolve(),
            Path("/tmp").resolve(),
            Path("/var/tmp").resolve(),
        }
        if self.path.parent in broad_roots:
            raise StorageCompatibilityError(
                "storage.path must identify a dedicated Cortex data directory"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        directory_stat = self.path.parent.stat()
        if hasattr(os, "getuid") and directory_stat.st_uid != os.getuid():
            raise StorageCompatibilityError("SQLite directory is not owned by the current user")
        try:
            self.path.parent.chmod(0o700)
        except OSError as exc:
            raise StorageReadOnlyError(
                "could not restrict SQLite directory to the current user"
            ) from exc
        if self.path.exists():
            file_stat = self.path.stat()
            if not stat.S_ISREG(file_stat.st_mode):
                raise StorageCompatibilityError("SQLite path exists but is not a regular file")
            if hasattr(os, "getuid") and file_stat.st_uid != os.getuid():
                raise StorageCompatibilityError("SQLite database is not owned by the current user")
            # Preserve an intentional owner read-only mode so the startup
            # write probe can report it, while stripping group/other access.
            restricted_mode = stat.S_IMODE(file_stat.st_mode) & 0o700
            try:
                self.path.chmod(restricted_mode)
            except OSError as exc:
                raise StorageReadOnlyError(
                    "could not restrict SQLite file to the current user"
                ) from exc
        if stat.S_IMODE(self.path.parent.stat().st_mode) & 0o077:
            raise StorageCompatibilityError(
                "SQLite directory permissions remain accessible to other users"
            )

    def _open_sync(self) -> None:
        if self._connection is not None:
            return
        self._worker_thread_id = threading.get_ident()
        version = _version_tuple(sqlite3.sqlite_version)
        if version < self._minimum_sqlite_version:
            raise StorageCompatibilityError(
                "Cortex requires SQLite >= "
                f"{'.'.join(map(str, self._minimum_sqlite_version))}; "
                f"runtime provides {sqlite3.sqlite_version}"
            )
        self._prepare_directory_sync()
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                self.path,
                timeout=self._busy_timeout_ms / 1_000,
                isolation_level=None,
                check_same_thread=True,
            )
            connection.row_factory = sqlite3.Row
            self._configure_connection_sync(connection)
            self._verify_identity_sync(connection)
            self._integrity_check_sync(connection, quick=True)
            self._apply_migrations_sync(connection)
            self._write_probe_sync(connection)
            self._integrity_check_sync(connection, quick=False)
            try:
                self.path.chmod(0o600)
            except OSError:
                logger.warning(
                    "Could not restrict SQLite file permissions at %s",
                    self.path,
                    exc_info=True,
                )
            if stat.S_IMODE(self.path.stat().st_mode) & 0o077:
                raise StorageCompatibilityError(
                    "SQLite file permissions remain accessible to other users"
                )
            self._connection = connection
            self._last_error = None
        except StorageError as exc:
            self._last_error = str(exc)
            if connection is not None:
                connection.close()
            raise
        except (sqlite3.Error, OSError) as exc:
            mapped = _sqlite_error(exc, operation="startup")
            self._last_error = str(mapped)
            if connection is not None:
                connection.close()
            raise mapped from exc

    def _configure_connection_sync(self, connection: sqlite3.Connection) -> None:
        connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
            raise StorageCompatibilityError("SQLite foreign_keys could not be enabled")
        journal_mode = str(connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).lower()
        if journal_mode != "delete":
            raise StorageCompatibilityError(
                f"unsupported SQLite journal mode {journal_mode!r}; expected 'delete'"
            )
        connection.execute("PRAGMA synchronous=FULL")
        if int(connection.execute("PRAGMA synchronous").fetchone()[0]) != 2:
            raise StorageCompatibilityError("SQLite FULL synchronous mode unavailable")
        connection.execute("PRAGMA locking_mode=NORMAL")
        locking_mode = str(connection.execute("PRAGMA locking_mode").fetchone()[0]).lower()
        if locking_mode != "normal":
            raise StorageCompatibilityError(f"unsupported SQLite locking mode {locking_mode!r}")
        # Schema SQL is trusted only during our own migration execution.
        connection.execute("PRAGMA trusted_schema=OFF")
        # FAST removes deleted payload from b-tree pages without imposing a
        # full-file rewrite on every deletion. Explicit user erase additionally
        # runs VACUUM in the maintenance repository.
        connection.execute("PRAGMA secure_delete=FAST")

    def _verify_identity_sync(self, connection: sqlite3.Connection) -> None:
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        if application_id not in {0, CORTEX_APPLICATION_ID}:
            raise StorageCompatibilityError(
                "configured database belongs to a different application "
                f"(application_id={application_id})"
            )
        if application_id == 0:
            connection.execute(f"PRAGMA application_id={CORTEX_APPLICATION_ID}")
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if user_version > CURRENT_SCHEMA_VERSION:
            raise StorageCompatibilityError(
                f"database schema {user_version} is newer than supported "
                f"schema {CURRENT_SCHEMA_VERSION}; refusing a down-migration"
            )

    def _integrity_check_sync(
        self,
        connection: sqlite3.Connection,
        *,
        quick: bool,
    ) -> None:
        pragma = "quick_check" if quick else "integrity_check"
        rows = [str(row[0]) for row in connection.execute(f"PRAGMA {pragma}")]
        self._last_integrity_check_unix_ms = self._clock.unix_ms()
        if rows != ["ok"]:
            raise StorageCorruptionError(f"SQLite {pragma} failed: {'; '.join(rows[:10])}")

    @staticmethod
    def _write_probe_sync(connection: sqlite3.Connection) -> None:
        """Prove the configured location can create and roll back a journal."""

        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("CREATE TABLE __cortex_startup_write_probe(value INTEGER)")
            connection.execute("INSERT INTO __cortex_startup_write_probe(value) VALUES (1)")
            connection.execute("ROLLBACK")
        except (sqlite3.Error, OSError) as exc:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise _sqlite_error(exc, operation="startup write probe") from exc

    @staticmethod
    def _migration_statements(source: str) -> list[str]:
        """Split packaged SQL using SQLite's own completeness parser."""

        statements: list[str] = []
        buffer = ""
        for line in source.splitlines(keepends=True):
            buffer += line
            if sqlite3.complete_statement(buffer):
                statement = buffer.strip()
                if statement:
                    statements.append(statement)
                buffer = ""
        if buffer.strip():
            raise StorageCompatibilityError("packaged migration has incomplete SQL")
        return statements

    def _read_migration(self, version: int) -> tuple[str, str, str]:
        name = f"{version:04d}_initial.sql" if version == 1 else f"{version:04d}.sql"
        try:
            source = (
                resources.files("cortex.storage.migrations")
                .joinpath(name)
                .read_text(encoding="utf-8")
            )
        except (FileNotFoundError, ModuleNotFoundError) as exc:
            raise StorageCompatibilityError(
                f"packaged SQLite migration {name!r} is missing"
            ) from exc
        return name, source, _sha256_bytes(source.encode("utf-8"))

    def _apply_migrations_sync(self, connection: sqlite3.Connection) -> None:
        current = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current == CURRENT_SCHEMA_VERSION:
            self._verify_migration_checksums_sync(connection)
            return
        if current > 0:
            self._backup_sync(
                self.backup_dir / f"cortex.pre-schema-{current}.{self._clock.unix_ms()}.sqlite3",
                connection=connection,
            )
        for version in range(current + 1, CURRENT_SCHEMA_VERSION + 1):
            name, source, source_sha256 = self._read_migration(version)
            try:
                connection.execute("BEGIN IMMEDIATE")
                # trusted_schema remains OFF for normal operation; the schema
                # contains no application-defined functions or virtual tables.
                for statement in self._migration_statements(source):
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations"
                    "(version, name, source_sha256, applied_at_unix_ms) "
                    "VALUES (?, ?, ?, ?)",
                    (version, name, source_sha256, self._clock.unix_ms()),
                )
                connection.execute(f"PRAGMA user_version={version}")
                connection.execute("COMMIT")
            except (sqlite3.Error, OSError) as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise _sqlite_error(exc, operation=f"migration {name}") from exc
        self._verify_migration_checksums_sync(connection)

    def _verify_migration_checksums_sync(self, connection: sqlite3.Connection) -> None:
        """Cross-check the applied-migration ledger against the packaged SQL.

        D5: a ledger row whose ``source_sha256`` differs from the packaged
        file of the same version used to raise ``StorageCorruptionError``
        — so any whitespace or comment edit to a shipped ``.sql`` bricked
        every existing install at startup, even though the schema those
        installs carry is exactly the one the edited file still produces.
        The applied schema is already in place and was verified by
        ``quick_check``/``integrity_check``; a packaging difference is
        therefore logged as a compatibility warning. What still fails
        closed: a ledger that does not line up with ``user_version`` (a
        real inconsistency) and a ``user_version`` newer than this build
        knows (``_verify_identity_sync``). The packaged files themselves
        are pinned by ``test_packaged_migration_checksums_are_pinned`` so
        an accidental edit is caught in CI instead of in users' installs.
        """
        rows = connection.execute(
            "SELECT version, name, source_sha256 FROM schema_migrations ORDER BY version"
        ).fetchall()
        if len(rows) != CURRENT_SCHEMA_VERSION:
            raise StorageCorruptionError(
                "schema migration ledger count does not match user_version"
            )
        for row in rows:
            version = int(row["version"])
            if version > CURRENT_SCHEMA_VERSION:
                raise StorageCompatibilityError(
                    f"schema migration ledger lists version {version}, newer than "
                    f"supported schema {CURRENT_SCHEMA_VERSION}; refusing a down-migration"
                )
            name, _source, expected_sha = self._read_migration(version)
            if row["name"] != name or row["source_sha256"] != expected_sha:
                logger.warning(
                    "Packaged migration %s for schema %d differs from the applied "
                    "ledger row (applied name=%s sha256=%s, packaged sha256=%s); the "
                    "applied schema is already in place, treating this as a "
                    "packaging compatibility notice rather than corruption",
                    name,
                    version,
                    row["name"],
                    row["source_sha256"],
                    expected_sha,
                )

    def _require_connection(self) -> sqlite3.Connection:
        connection = self._connection
        if connection is None:
            raise StorageError("SQLite connection is not open")
        if threading.get_ident() != self._worker_thread_id:
            raise StorageError("SQLite connection accessed outside its owner thread")
        return connection

    async def read(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        """Run a read callback on the connection owner thread."""

        await self.start()

        def invoke() -> T:
            return operation(self._require_connection())

        return await self._submit_raw(invoke, critical=False)

    async def transaction(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        """Run one durable ``BEGIN IMMEDIATE`` transaction.

        Cancellation is delayed until SQLite has committed or rolled back, so
        callers never leave an unknown half-transaction merely because their
        asyncio parent was cancelled during shutdown.
        """

        await self.start()

        def invoke() -> T:
            connection = self._require_connection()
            try:
                connection.execute("BEGIN IMMEDIATE")
                result = operation(connection)
                connection.execute("COMMIT")
                self._last_error = None
                return result
            except StorageError:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            except (sqlite3.Error, OSError) as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                mapped = _sqlite_error(exc, operation="transaction")
                self._last_error = str(mapped)
                raise mapped from exc
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

        return await self._submit_raw(invoke, critical=True)

    async def maintenance(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        """Run a serialized connection-level operation outside a transaction.

        SQLite commands such as ``VACUUM`` and ``PRAGMA optimize`` reject an
        enclosing transaction. Only the storage maintenance service should use
        this boundary.
        """

        await self.start()

        def invoke() -> T:
            try:
                return operation(self._require_connection())
            except StorageError:
                raise
            except (sqlite3.Error, OSError) as exc:
                mapped = _sqlite_error(exc, operation="maintenance")
                self._last_error = str(mapped)
                raise mapped from exc

        return await self._submit_raw(invoke, critical=True)

    @staticmethod
    def _reject_event_loop_thread(method: str) -> None:
        """D9: refuse to block a running asyncio loop with SQLite work.

        ``asyncio.get_running_loop`` only succeeds on the thread that is
        running a loop, so a caller dispatched through
        ``asyncio.to_thread`` (or any plain worker thread) passes while a
        coroutine calling the sync bridge directly is rejected instead of
        silently freezing every WS client, HTTP request and capture tick
        for the duration of the call (up to the busy timeout).
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        raise StorageError(
            f"SQLiteDatabase.{method} was called from a thread running an asyncio "
            "event loop; it would block the loop for the duration of the SQLite "
            "call. Use the async API (read/transaction) or run the caller with "
            "asyncio.to_thread()."
        )

    def call_sync(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        """Run a callback synchronously after async startup.

        This narrow bridge exists for legacy synchronous calibration readers.
        It never opens a second connection. New application services should
        use :meth:`read` or :meth:`transaction`. It must never be invoked
        from the event-loop thread (D9) — see :meth:`_reject_event_loop_thread`.
        """

        self._reject_event_loop_thread("call_sync")
        if self._closed:
            raise StorageError("call_sync requires an open database")
        if not self._started:
            self.start_sync()
        if threading.get_ident() == self._worker_thread_id:
            return operation(self._require_connection())
        future: Future[T] = self._executor.submit(lambda: operation(self._require_connection()))
        return future.result(timeout=max(5.0, self._busy_timeout_ms / 1_000 + 1.0))

    def transaction_sync(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        """Synchronous compatibility bridge with atomic commit semantics.

        Same event-loop-thread guard as :meth:`call_sync` (D9).
        """

        self._reject_event_loop_thread("transaction_sync")

        def invoke(connection: sqlite3.Connection) -> T:
            try:
                connection.execute("BEGIN IMMEDIATE")
                result = operation(connection)
                connection.execute("COMMIT")
                self._last_error = None
                return result
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

        return self.call_sync(invoke)

    async def _submit_raw(self, operation: Callable[[], T], *, critical: bool) -> T:
        if self._closed:
            raise StorageError("SQLiteDatabase is closed")
        loop = asyncio.get_running_loop()
        with self._pending_lock:
            self._pending_operations += 1
        future = loop.run_in_executor(self._executor, operation)
        try:
            if not critical:
                return await future
            try:
                return await asyncio.shield(future)
            except asyncio.CancelledError:
                # The transaction has already entered the worker queue. Wait
                # for its atomic outcome before propagating cancellation.
                await asyncio.shield(future)
                raise
        finally:
            with self._pending_lock:
                self._pending_operations = max(0, self._pending_operations - 1)

    async def backup(self, destination: str | Path) -> Path:
        """Create and verify an online backup using SQLite's backup API."""

        await self.start()
        target = Path(destination).expanduser().resolve()
        if target == self.path or target.is_dir():
            raise ValueError("backup destination must be a file distinct from the database")

        def invoke() -> Path:
            return self._backup_sync(target, connection=self._require_connection())

        return await self._submit_raw(invoke, critical=True)

    def _backup_sync(
        self,
        destination: Path,
        *,
        connection: sqlite3.Connection,
    ) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        target: sqlite3.Connection | None = None
        try:
            target = sqlite3.connect(temp, isolation_level=None)
            connection.backup(target)
            rows = [str(row[0]) for row in target.execute("PRAGMA integrity_check")]
            if rows != ["ok"]:
                raise StorageCorruptionError(
                    f"new SQLite backup failed integrity_check: {rows[:10]}"
                )
            target.close()
            target = None
            temp.chmod(0o600)
            os.replace(temp, destination)
            self._fsync_directory(destination.parent)
        except BaseException:
            if target is not None:
                target.close()
            try:
                temp.unlink()
            except OSError:
                pass
            raise
        self._prune_backups_sync()
        return destination

    def _prune_backups_sync(self) -> None:
        if not self.backup_dir.exists():
            return
        candidates = sorted(
            (
                path
                for path in self.backup_dir.glob("cortex.pre-schema-*.sqlite3")
                if path.is_file()
            ),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        for stale in candidates[self._backup_retention_count :]:
            try:
                stale.unlink()
            except OSError:
                logger.warning("Could not prune stale SQLite backup %s", stale)

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        if not hasattr(os, "O_DIRECTORY"):
            return
        try:
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    async def health(self, *, full_integrity_check: bool = False) -> dict[str, Any]:
        """Return a path-redacted health snapshot backed by a live check."""

        try:
            await self.start()

            def inspect(connection: sqlite3.Connection) -> dict[str, Any]:
                check = "integrity_check" if full_integrity_check else "quick_check"
                rows = [str(row[0]) for row in connection.execute(f"PRAGMA {check}")]
                healthy = rows == ["ok"]
                self._last_integrity_check_unix_ms = self._clock.unix_ms()
                if not healthy:
                    self._last_error = f"SQLite {check} failed"
                counts = {
                    str(row[0]): int(row[1])
                    for row in connection.execute(
                        "SELECT 'interventions', COUNT(*) FROM intervention_transactions "
                        "UNION ALL SELECT 'receipts', COUNT(*) FROM intervention_receipts "
                        "UNION ALL SELECT 'policy_decisions', COUNT(*) FROM policy_decisions "
                        "UNION ALL SELECT 'sessions', COUNT(*) FROM session_aggregates"
                    )
                }
                return {
                    "healthy": healthy,
                    "degraded": not healthy,
                    "backend": "sqlite",
                    "journal_mode": str(
                        connection.execute("PRAGMA journal_mode").fetchone()[0]
                    ).lower(),
                    "synchronous": "full"
                    if int(connection.execute("PRAGMA synchronous").fetchone()[0]) == 2
                    else "unsupported",
                    "foreign_keys": bool(
                        int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
                    ),
                    "schema_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
                    "sqlite_version": sqlite3.sqlite_version,
                    "database_filename": self.path.name,
                    "database_bytes": self.path.stat().st_size if self.path.exists() else 0,
                    "pending_operations": self.pending_operations,
                    "last_integrity_check_unix_ms": self._last_integrity_check_unix_ms,
                    "last_error": self._last_error,
                    "record_counts": counts,
                }

            return await self.read(inspect)
        except StorageError as exc:
            return {
                "healthy": False,
                "degraded": True,
                "backend": "sqlite",
                "journal_mode": "unavailable",
                "synchronous": "unavailable",
                "foreign_keys": False,
                "schema_version": 0,
                "sqlite_version": sqlite3.sqlite_version,
                "database_filename": self.path.name,
                "database_bytes": self.path.stat().st_size if self.path.exists() else 0,
                "pending_operations": self.pending_operations,
                "last_integrity_check_unix_ms": self._last_integrity_check_unix_ms,
                "last_error": str(exc),
                "record_counts": {},
            }

    async def close(self) -> None:
        """Drain queued operations, close the connection, and stop its thread."""

        if self._closed:
            return
        async with self._close_lock:
            if self._closed:
                return
            if self._started:

                def close_sync() -> None:
                    connection = self._require_connection()
                    try:
                        connection.execute("PRAGMA optimize")
                    finally:
                        connection.close()
                        self._connection = None

                await self._submit_raw(close_sync, critical=True)
            self._closed = True
            # D9: ``ThreadPoolExecutor.shutdown(wait=True)`` joins the
            # worker thread. Joining on the event loop stalled every
            # other coroutine for as long as the worker took to drain
            # (a queued VACUUM or integrity_check can be seconds), so the
            # join runs off-loop and is bounded; a worker that does not
            # exit in time is abandoned to process exit rather than
            # holding the shutdown chain hostage.
            join_timeout = max(5.0, self._busy_timeout_ms / 1_000 + 1.0)
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(
                        self._executor.shutdown, wait=True, cancel_futures=False
                    ),
                    timeout=join_timeout,
                )
            except TimeoutError:
                logger.warning(
                    "SQLite worker thread did not exit within %.1fs; abandoning the "
                    "join so shutdown can proceed",
                    join_timeout,
                )


__all__ = [
    "CORTEX_APPLICATION_ID",
    "CURRENT_SCHEMA_VERSION",
    "DEFAULT_SESSION_RETENTION_DAYS",
    "MINIMUM_SQLITE_VERSION",
    "SQLiteDatabase",
    "StorageBusyError",
    "StorageCapacityError",
    "StorageCompatibilityError",
    "StorageCorruptionError",
    "StorageError",
    "StorageReadOnlyError",
]
