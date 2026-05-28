# Store – async key-value and timeseries storage
#
# Public entry points:
#   * :class:`InMemoryStore` — dict-backed; optional file-backed
#     persistence via the ``persist_path`` kwarg.
#   * :class:`RedisStore` — production-grade backend when Redis is
#     reachable.
#   * :func:`make_default_store` — convenience selector that returns
#     ``RedisStore`` when configured + reachable, else
#     ``InMemoryStore`` with an OS-appropriate persistence path so
#     consent / calibration state survives daemon restarts in the
#     DMG-default (no-Redis) deployment.

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from cortex.libs.store.memory_store import InMemoryStore
from cortex.libs.store.redis_store import RedisStore


def _default_persist_path() -> Path:
    """OS-appropriate path for the in-memory store's persisted JSON.

    * macOS (Darwin): ``~/Library/Application Support/Cortex/store.json``
      — matches the platform convention and is the same parent dir the
      rest of Cortex's user-data lives in.
    * Everywhere else: ``~/.cortex/store.json`` — a dotted directory
      under ``$HOME`` keeps Linux / Windows / unknown platforms quiet.

    The parent directory is created lazily by :func:`atomic_write_json`
    when the store first writes, so callers do not need to ``mkdir``
    before constructing the store.
    """
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Cortex" / "store.json"
    return home / ".cortex" / "store.json"


def make_default_store(
    config: Any | None = None,
    *,
    persist_path: Path | None = None,
) -> InMemoryStore | RedisStore:
    """Return the default store for the active deployment.

    The DMG ships without Redis (no service to manage, no extra
    surface), so the no-Redis path MUST persist. Without persistence,
    ``ConsentLadder._persist → store.set_json("consent_ladder_state",
    ...)`` is dropped on every daemon restart and the user's earned
    consent escalations are silently reset — a P1 audit finding.

    Selection logic
    ---------------

    The helper accepts the full ``CortexConfig`` and reads its ``redis``
    sub-config — if ``redis.enabled is True`` we return a
    :class:`RedisStore` against the configured host/port/db. Otherwise
    we return :class:`InMemoryStore` with ``persist_path`` set to the
    explicit argument, falling back to :func:`_default_persist_path`
    when callers don't supply one.

    The wide ``Any`` annotation on ``config`` keeps the helper tolerant
    of duck-typed stubs in tests (``SimpleNamespace`` etc.) without
    coupling this module to the full settings layer. ``None`` selects
    the in-memory + default-path branch which is the right behaviour
    for the DMG-default deployment.
    """
    # Best-effort introspection. ``getattr`` chains keep the helper
    # tolerant of stubs, ``None``, or partial configs — any missing
    # attribute falls through to the in-memory branch.
    redis_cfg = getattr(config, "redis", None) if config is not None else None
    redis_enabled = bool(getattr(redis_cfg, "enabled", False))
    key_prefix = (
        getattr(redis_cfg, "key_prefix", "cortex") or "cortex"
    ) if redis_cfg is not None else "cortex"

    if redis_enabled and redis_cfg is not None:
        return RedisStore(
            host=getattr(redis_cfg, "host", "localhost"),
            port=int(getattr(redis_cfg, "port", 6379)),
            db=int(getattr(redis_cfg, "db", 0)),
            key_prefix=key_prefix,
        )

    return InMemoryStore(
        key_prefix=key_prefix,
        persist_path=persist_path or _default_persist_path(),
    )


__all__ = [
    "InMemoryStore",
    "RedisStore",
    "make_default_store",
]
