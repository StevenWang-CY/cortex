"""Intervention port — abstract capability surface for the engine.

The api_gateway (HTTP routes + WebSocket handlers) currently imports
``capture_snapshot`` and ``prepare_plan`` directly from
``cortex.services.intervention_engine``. That couples the gateway to
the concrete engine implementation. The :class:`InterventionPort`
Protocol declared here lets the gateway depend on a *capability* — a
duck-typed object that exposes the two callables — so a fake / stub
can be wired in tests without monkey-patching the import system.

The concrete engine continues to provide module-level
``capture_snapshot`` / ``prepare_plan`` functions for backwards
compatibility; this port is purely additive.

Backwards compatibility
-----------------------

Until the gateway is migrated (Phase-4b, owned by a downstream agent),
the import chain remains:

    routes.py
        ↓ from cortex.services.intervention_engine import capture_snapshot, prepare_plan

After migration:

    routes.py
        ↓ from cortex.libs.ports import InterventionPort  # type hint only
        ↓ port: InterventionPort = ...  # injected at app construction time

No code outside ``routes.py`` / ``websocket_server.py`` references this
Protocol today; it's introduced ahead of those edits so the contract
is committed and reviewable in isolation.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from cortex.libs.schemas.context import TaskContext
from cortex.libs.schemas.intervention import (
    AdapterCommand,
    InterventionPlan,
    ValidationResult,
    WorkspaceSnapshot,
)


@runtime_checkable
class InterventionPort(Protocol):
    """Capability surface the api_gateway consumes from the engine.

    Implementations MUST be safe to call from an asyncio event loop
    thread — both methods are CPU-bound and synchronous in the current
    engine, so the gateway calls them directly without
    ``loop.run_in_executor``. If a future implementation grows I/O,
    introduce an async variant rather than changing this signature.

    Methods are intentionally module-level functions on the current
    engine (``cortex.services.intervention_engine.snapshot.capture_snapshot``,
    ``cortex.services.intervention_engine.planner.prepare_plan``); this
    Protocol shapes them as instance methods so a single dependency-
    injected object can satisfy the whole port.
    """

    def capture_snapshot(
        self,
        context: TaskContext | None = None,
        intervention_id: str | None = None,
        *,
        timestamp: float | None = None,
    ) -> WorkspaceSnapshot:
        """Snapshot the live workspace state so the plan can be
        restored after the intervention ends. See
        ``cortex.services.intervention_engine.snapshot.capture_snapshot``
        for the current implementation; the gateway calls this exactly
        once per intervention.
        """
        ...

    def prepare_plan(
        self,
        plan: InterventionPlan,
        *,
        tab_count: int | None = None,
    ) -> tuple[ValidationResult, list[AdapterCommand]]:
        """Validate + lower the LLM-produced plan into a list of
        ``AdapterCommand`` ready for the executor. Returns the
        validation result so the gateway can surface warnings (e.g.
        "your plan was trimmed to fit the safety policy") even on
        success. See
        ``cortex.services.intervention_engine.planner.prepare_plan``
        for the current implementation.
        """
        ...


__all__ = ["InterventionPort"]
