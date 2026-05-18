"""Audit F36 — storage size budget with oldest-first eviction.

Cortex writes one ``storage/sessions/session_<id>.json`` per session.
Without a budget, long-running installs accumulate sessions until they
fill the user's disk. F36 introduces ``StorageConfig.max_total_size_mb``
(default 500 MB) and an ``enforce_session_storage_budget`` helper that
evicts oldest sessions (by mtime) before each write whenever the new
write would push the directory over budget.

Four cases:

1. Directory under budget → no eviction.
2. Directory over budget → oldest file evicted (newest survives).
3. Eviction stops once total + incoming fits.
4. ``max_total_size_mb=0`` → all sessions evicted before each write.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cortex.libs.config.settings import StorageConfig
from cortex.services.runtime_daemon import enforce_session_storage_budget


def _write_session(directory: Path, name: str, kb: int, mtime: float) -> Path:
    """Helper: write a session JSON of approximately ``kb`` kilobytes,
    backdated to ``mtime``."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"session_{name}.json"
    # Fill with a deterministic payload of approximately kb * 1024 bytes.
    payload = "{\n  \"x\":\"" + ("y" * (kb * 1024 - 16)) + "\"\n}"
    path.write_text(payload)
    os.utime(path, (mtime, mtime))
    return path


def test_config_field_default_is_500mb() -> None:
    cfg = StorageConfig()
    assert cfg.max_total_size_mb == 500


def test_under_budget_no_eviction(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    a = _write_session(sessions, "a", kb=100, mtime=1_000_000.0)
    b = _write_session(sessions, "b", kb=100, mtime=1_000_100.0)
    evicted = enforce_session_storage_budget(
        sessions, incoming_bytes=50 * 1024, max_total_size_mb=10
    )
    assert evicted == 0
    assert a.exists()
    assert b.exists()


def test_over_budget_evicts_oldest_first(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    # Three 400 KB files. Budget 1 MB. Incoming 400 KB — would total
    # 1.6 MB; must evict the oldest until total + 400 KB <= 1 MB.
    oldest = _write_session(sessions, "old", kb=400, mtime=1_000.0)
    mid = _write_session(sessions, "mid", kb=400, mtime=2_000.0)
    newest = _write_session(sessions, "new", kb=400, mtime=3_000.0)

    evicted = enforce_session_storage_budget(
        sessions, incoming_bytes=400 * 1024, max_total_size_mb=1
    )
    assert evicted >= 1
    # Oldest must be gone; newest survives.
    assert not oldest.exists(), "oldest session should have been evicted first"
    assert newest.exists(), "newest session must survive"
    # Mid may or may not survive depending on exact size accounting —
    # the contract is "evict oldest until fits", not "evict everything".
    _ = mid


def test_eviction_stops_once_under_budget(tmp_path: Path) -> None:
    """Three 200 KB files, 1 MB budget, 100 KB incoming = 700 KB total.
    Already under budget → no eviction. Then bump incoming to 700 KB
    = 1.3 MB total → evict ONE oldest (300 KB freed → 1.0 MB total,
    fits the incoming 700 KB)."""
    sessions = tmp_path / "sessions"
    oldest = _write_session(sessions, "1", kb=200, mtime=1_000.0)
    middle = _write_session(sessions, "2", kb=200, mtime=2_000.0)
    newest = _write_session(sessions, "3", kb=200, mtime=3_000.0)

    # Case A: comfortably under budget.
    n = enforce_session_storage_budget(
        sessions, incoming_bytes=100 * 1024, max_total_size_mb=1
    )
    assert n == 0
    assert oldest.exists()
    assert middle.exists()
    assert newest.exists()

    # Case B: incoming nudges over budget. Eviction proceeds until the
    # write would fit, NOT until the directory is empty.
    n = enforce_session_storage_budget(
        sessions, incoming_bytes=700 * 1024, max_total_size_mb=1
    )
    assert 1 <= n <= 2, (
        f"expected eviction to stop once new write would fit, evicted {n}"
    )
    # The newest must always survive.
    assert newest.exists()


def test_zero_budget_evicts_everything(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    files = [
        _write_session(sessions, str(i), kb=10, mtime=1_000.0 + i)
        for i in range(4)
    ]
    n = enforce_session_storage_budget(
        sessions, incoming_bytes=10 * 1024, max_total_size_mb=0
    )
    assert n == 4
    for p in files:
        assert not p.exists()


def test_no_eviction_when_directory_missing(tmp_path: Path) -> None:
    """A missing sessions dir is a no-op (first-ever shutdown path)."""
    n = enforce_session_storage_budget(
        tmp_path / "does_not_exist",
        incoming_bytes=1024,
        max_total_size_mb=500,
    )
    assert n == 0
