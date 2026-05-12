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
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from cortex.libs.config.settings import StorageConfig

logger = logging.getLogger(__name__)


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

    for path in directory.rglob("*"):
        if path.is_dir():
            continue
        scanned += 1
        try:
            mtime = path.stat().st_mtime
        except OSError:
            errors += 1
            continue
        if now - mtime <= retention_seconds:
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
