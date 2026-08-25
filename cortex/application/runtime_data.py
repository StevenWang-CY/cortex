"""Typed current-value port shared by runtime coordinators."""

from __future__ import annotations

from dataclasses import dataclass, replace
from threading import RLock
from typing import Any


@dataclass(frozen=True, slots=True)
class RuntimeDataSnapshot:
    state_estimate: Any | None = None
    telemetry: Any | None = None
    feature_vector: Any | None = None
    task_context: Any | None = None


class RuntimeDataPort:
    """Thread-safe typed projection of coordinator-produced current data."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._snapshot = RuntimeDataSnapshot()
        self._workspace_snapshots: dict[str, Any | None] = {}

    def snapshot(self) -> RuntimeDataSnapshot:
        with self._lock:
            return self._snapshot

    def publish_state_estimate(self, value: Any) -> None:
        self._replace(state_estimate=value)

    def publish_telemetry(self, value: Any) -> None:
        self._replace(telemetry=value)

    def publish_feature_vector(self, value: Any) -> None:
        self._replace(feature_vector=value)

    def publish_task_context(self, value: Any) -> None:
        self._replace(task_context=value)

    def put_workspace_snapshot(self, intervention_id: str, value: Any | None) -> None:
        if not intervention_id:
            raise ValueError("intervention_id must be non-empty")
        with self._lock:
            self._workspace_snapshots[intervention_id] = value

    def workspace_snapshot(self, intervention_id: str) -> tuple[bool, Any | None]:
        with self._lock:
            if intervention_id not in self._workspace_snapshots:
                return False, None
            return True, self._workspace_snapshots[intervention_id]

    def reset(self) -> None:
        with self._lock:
            self._snapshot = RuntimeDataSnapshot()
            self._workspace_snapshots.clear()

    def _replace(self, **changes: Any) -> None:
        with self._lock:
            self._snapshot = replace(self._snapshot, **changes)
