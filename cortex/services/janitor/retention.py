"""Retention sweep (G.2).

Cortex collects three families of on-disk artifacts:

* ``storage/sessions/`` — JSONL telemetry + per-session reports.
* ``storage/cache/`` and ``storage/exports/`` — derived feature caches.
* ``storage/logs/`` — daemon logs.

``StorageConfig`` exposes three retention windows (``session_retention_days``,
``feature_retention_days``, ``error_retention_days``) which were previously
declarative-only — no code read them, so old files accumulated forever.

``sweep_once`` walks the configured directories, deletes files older than
their retention window, and returns the counts. The daemon runs it daily
via ``runtime_daemon._retention_sweep_loop``.

F35: ``sweep_once_async`` is the event-loop-friendly variant. Big storage
roots (5 k+ files) made the synchronous ``rglob`` + per-file ``stat``
loop saturate the asyncio thread it was offloaded onto, starving any
co-resident I/O bound coroutines. The async variant caps work at
``_FILES_PER_TICK`` files per tick and yields back to the event loop
between chunks so the state coroutine, telemetry coroutine, etc. all
continue ticking during a long sweep.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from cortex.libs.config.settings import StorageConfig

logger = logging.getLogger(__name__)

# F35: per-tick file budget. 1 000 files per chunk balances throughput
# (one ``await asyncio.sleep(0)`` per ~10 ms of stat/unlink work) against
# event-loop responsiveness. Smaller values starve the sweep; larger
# values reintroduce the original latency hump.
_FILES_PER_TICK = 1000


@dataclass(frozen=True)
class SweepResult:
    """Summary of a single retention pass."""

    files_scanned: int = 0
    files_deleted: int = 0
    bytes_freed: int = 0
    errors: int = 0


def _sweep_directory(
    directory: Path,
    *,
    retention_seconds: float,
    now: float,
) -> SweepResult:
    """Delete files older than ``retention_seconds`` in ``directory``.

    Subdirectories are walked recursively. Symlinks are not followed
    (PathLib ``rglob`` doesn't follow them by default). Empty
    subdirectories left after deletion are also pruned.
    """
    if not directory.exists() or not directory.is_dir():
        return SweepResult()

    scanned = 0
    deleted = 0
    freed = 0
    errors = 0

    # swiftdata-pro rule (transferred): tombstone partial-write ``*.tmp``
    # files older than 24h, regardless of the directory's overall retention
    # window. These are SQLite/JSON write-ahead artifacts that should never
    # outlive a normal session; if they survive a day, the writer crashed.
    _TMP_TOMBSTONE_SECONDS = 24 * 3600

    for path in directory.rglob("*"):
        if path.is_dir():
            continue
        scanned += 1
        try:
            mtime = path.stat().st_mtime
        except OSError:
            errors += 1
            continue
        is_tmp = path.suffix == ".tmp"
        threshold = (
            _TMP_TOMBSTONE_SECONDS if is_tmp else retention_seconds
        )
        if now - mtime <= threshold:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        try:
            path.unlink()
            deleted += 1
            freed += size
        except OSError:
            errors += 1

    # Prune empty subdirectories left behind.
    for sub in sorted(directory.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if sub.is_dir():
            try:
                next(sub.iterdir())
            except StopIteration:
                try:
                    sub.rmdir()
                except OSError:
                    pass
            except OSError:
                pass

    return SweepResult(
        files_scanned=scanned,
        files_deleted=deleted,
        bytes_freed=freed,
        errors=errors,
    )


def sweep_once(
    config: StorageConfig,
    *,
    storage_root: Path,
    now: float | None = None,
) -> dict[str, SweepResult]:
    """Run a single retention pass across all known directories.

    Args:
        config: ``StorageConfig`` carrying the per-class retention days.
        storage_root: The user's ``CORTEX_STORAGE__PATH`` (already expanded).
        now: Override the current time for tests.

    Returns:
        Mapping of subdirectory name → ``SweepResult``.
    """
    now_ts = now if now is not None else time.time()
    day = 86400.0
    results: dict[str, SweepResult] = {}

    targets: list[tuple[str, float]] = [
        ("sessions", config.session_retention_days * day),
        ("cache", config.feature_retention_days * day),
        ("exports", config.feature_retention_days * day),
        ("logs", config.error_retention_days * day),
    ]

    for name, retention_seconds in targets:
        sub = storage_root / name
        result = _sweep_directory(sub, retention_seconds=retention_seconds, now=now_ts)
        results[name] = result
        if result.files_deleted > 0 or result.errors > 0:
            logger.info(
                "retention.sweep dir=%s scanned=%d deleted=%d freed=%d errors=%d",
                name,
                result.files_scanned,
                result.files_deleted,
                result.bytes_freed,
                result.errors,
            )
    return results


async def _sweep_directory_async(
    directory: Path,
    *,
    retention_seconds: float,
    now: float,
) -> SweepResult:
    """Async variant of :func:`_sweep_directory` (F35).

    The expensive ``Path.rglob`` walk + per-file ``stat`` and ``unlink``
    calls are pushed onto :func:`asyncio.to_thread` so they do not run
    on the event loop. We process ``_FILES_PER_TICK`` files per offloaded
    chunk and yield back to the loop (``await asyncio.sleep(0)``) between
    chunks so other coroutines progress while a large sweep is in flight.
    """
    if not directory.exists() or not directory.is_dir():
        return SweepResult()

    _TMP_TOMBSTONE_SECONDS = 24 * 3600

    # Snapshot the path list off-thread so the rglob walk itself doesn't
    # block the event loop. A list of paths for 5 000 files is only a
    # few hundred KB — comfortable for in-memory.
    def _list_files() -> list[Path]:
        return [p for p in directory.rglob("*") if not p.is_dir()]

    paths = await asyncio.to_thread(_list_files)

    scanned = 0
    deleted = 0
    freed = 0
    errors = 0

    def _process_chunk(chunk: list[Path]) -> tuple[int, int, int, int]:
        c_scanned = 0
        c_deleted = 0
        c_freed = 0
        c_errors = 0
        for path in chunk:
            c_scanned += 1
            try:
                mtime = path.stat().st_mtime
            except OSError:
                c_errors += 1
                continue
            is_tmp = path.suffix == ".tmp"
            threshold = (
                _TMP_TOMBSTONE_SECONDS if is_tmp else retention_seconds
            )
            if now - mtime <= threshold:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            try:
                path.unlink()
                c_deleted += 1
                c_freed += size
            except OSError:
                c_errors += 1
        return c_scanned, c_deleted, c_freed, c_errors

    for offset in range(0, len(paths), _FILES_PER_TICK):
        chunk = paths[offset : offset + _FILES_PER_TICK]
        c_scanned, c_deleted, c_freed, c_errors = await asyncio.to_thread(
            _process_chunk, chunk
        )
        scanned += c_scanned
        deleted += c_deleted
        freed += c_freed
        errors += c_errors
        # Yield to the event loop between chunks so co-resident
        # coroutines (state loop, telemetry loop, broadcast loop) tick.
        await asyncio.sleep(0)

    # Prune empty subdirectories off-thread as well.
    def _prune_empty() -> None:
        for sub in sorted(
            directory.rglob("*"), key=lambda p: len(p.parts), reverse=True
        ):
            if sub.is_dir():
                try:
                    next(sub.iterdir())
                except StopIteration:
                    try:
                        sub.rmdir()
                    except OSError:
                        pass
                except OSError:
                    pass

    await asyncio.to_thread(_prune_empty)

    return SweepResult(
        files_scanned=scanned,
        files_deleted=deleted,
        bytes_freed=freed,
        errors=errors,
    )


async def sweep_once_async(
    config: StorageConfig,
    *,
    storage_root: Path,
    now: float | None = None,
) -> dict[str, SweepResult]:
    """Async variant of :func:`sweep_once` (F35).

    Caps work at :data:`_FILES_PER_TICK` per chunk and yields the event
    loop between chunks so a large sweep does not starve co-resident
    coroutines.
    """
    now_ts = now if now is not None else time.time()
    day = 86400.0
    results: dict[str, SweepResult] = {}

    targets: list[tuple[str, float]] = [
        ("sessions", config.session_retention_days * day),
        ("cache", config.feature_retention_days * day),
        ("exports", config.feature_retention_days * day),
        ("logs", config.error_retention_days * day),
    ]

    for name, retention_seconds in targets:
        sub = storage_root / name
        result = await _sweep_directory_async(
            sub, retention_seconds=retention_seconds, now=now_ts
        )
        results[name] = result
        if result.files_deleted > 0 or result.errors > 0:
            logger.info(
                "retention.sweep dir=%s scanned=%d deleted=%d freed=%d errors=%d",
                name,
                result.files_scanned,
                result.files_deleted,
                result.bytes_freed,
                result.errors,
            )
    return results
