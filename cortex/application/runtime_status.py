"""Typed, instance-scoped status port consumed by transport adapters."""

from __future__ import annotations

from dataclasses import dataclass, replace
from threading import RLock
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class RuntimeStatusSnapshot:
    daemon: Any | None = None
    latest_frame_meta: Any | None = None
    capture_stale: bool = False
    store_degraded: bool = False
    store_backend: str | None = None
    store_healthy: bool | None = None


class RuntimeStatusReader(Protocol):
    def snapshot(self) -> RuntimeStatusSnapshot: ...


class RuntimeStatusPort:
    """Small typed projection of mutable runtime health.

    WebSocket presentation needs only these fields. Keeping them outside the
    compatibility service dictionary prevents the transport from discovering
    the daemon or health state by magic string.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._snapshot = RuntimeStatusSnapshot()

    def snapshot(self) -> RuntimeStatusSnapshot:
        with self._lock:
            return self._snapshot

    def bind_daemon(self, daemon: Any) -> None:
        self._replace(daemon=daemon)

    def publish_frame(self, frame_meta: Any) -> None:
        self._replace(latest_frame_meta=frame_meta, capture_stale=False)

    def mark_capture_stale(self, stale: bool = True) -> None:
        self._replace(capture_stale=bool(stale))

    def publish_storage(
        self,
        *,
        degraded: bool,
        backend: str | None,
        healthy: bool | None,
    ) -> None:
        self._replace(
            store_degraded=bool(degraded),
            store_backend=backend,
            store_healthy=healthy,
        )

    def reset(self) -> None:
        with self._lock:
            self._snapshot = RuntimeStatusSnapshot()

    def _replace(self, **changes: Any) -> None:
        with self._lock:
            self._snapshot = replace(self._snapshot, **changes)
