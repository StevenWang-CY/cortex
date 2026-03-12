"""
Intervention Engine — Executor

Applies an intervention plan to the workspace by dispatching adapter
commands to VS Code, Chrome, terminal, and desktop overlay. Tracks
all mutations for later restoration.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from cortex.libs.schemas.intervention import InterventionPlan
from cortex.services.intervention_engine.planner import AdapterCommand

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Adapter protocol
# ---------------------------------------------------------------------------


class WorkspaceAdapter(Protocol):
    """Protocol for workspace adapters that can execute commands."""

    async def execute(self, action: str, params: dict[str, Any]) -> bool:
        """Execute an adapter command. Returns True on success."""
        ...


# ---------------------------------------------------------------------------
# Mutation tracking
# ---------------------------------------------------------------------------


@dataclass
class Mutation:
    """Record of a single workspace mutation."""

    adapter: str
    action: str
    params: dict[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0
    success: bool = False
    reverse_action: str | None = None

    @property
    def is_reversible(self) -> bool:
        """Check if this mutation can be reversed."""
        return self.reverse_action is not None


# Action → reverse action mapping
_REVERSE_ACTIONS: dict[str, str] = {
    "hide_tabs_except_active": "show_all_tabs",
    "collapse_before_error": "expand_terminal",
    "fold_except_current": "unfold_all",
    "dim_background": "remove_dim",
    "show_overlay": "hide_overlay",
}


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class InterventionExecutor:
    """
    Executes an intervention plan by dispatching commands to adapters.

    Tracks all mutations for restoration. Ensures no destructive operations.
    """

    def __init__(self) -> None:
        self._adapters: dict[str, WorkspaceAdapter] = {}
        self._active_mutations: dict[str, list[Mutation]] = {}
        # intervention_id → list of mutations

    def register_adapter(self, name: str, adapter: WorkspaceAdapter) -> None:
        """Register a workspace adapter (editor, browser, terminal, overlay)."""
        self._adapters[name] = adapter

    def has_adapter(self, name: str) -> bool:
        """Check if an adapter is registered."""
        return name in self._adapters

    async def apply(
        self,
        plan: InterventionPlan,
        commands: list[AdapterCommand],
        *,
        timestamp: float | None = None,
    ) -> list[Mutation]:
        """
        Apply intervention commands to workspace adapters.

        Args:
            plan: The intervention plan being applied.
            commands: Mapped adapter commands from the planner.
            timestamp: Override for testing.

        Returns:
            List of mutations that were applied (both successful and failed).
        """
        now = timestamp if timestamp is not None else time.monotonic()
        mutations: list[Mutation] = []

        for cmd in commands:
            mutation = Mutation(
                adapter=cmd.adapter,
                action=cmd.action,
                params=dict(cmd.params),
                timestamp=now,
                reverse_action=_REVERSE_ACTIONS.get(cmd.action),
            )

            adapter = self._adapters.get(cmd.adapter)
            if adapter is None:
                logger.warning(
                    "No adapter registered for '%s', skipping %s",
                    cmd.adapter,
                    cmd.action,
                )
                mutation.success = False
                mutations.append(mutation)
                continue

            try:
                success = await adapter.execute(cmd.action, cmd.params)
                mutation.success = success
                if not success:
                    logger.warning(
                        "Adapter '%s' failed to execute '%s'",
                        cmd.adapter,
                        cmd.action,
                    )
            except Exception:
                logger.exception(
                    "Error executing '%s' on adapter '%s'",
                    cmd.action,
                    cmd.adapter,
                )
                mutation.success = False

            mutations.append(mutation)

        # Store mutations by intervention_id
        self._active_mutations[plan.intervention_id] = mutations
        return mutations

    async def reverse(self, intervention_id: str) -> list[Mutation]:
        """
        Reverse all mutations for a given intervention.

        Returns list of reversal mutations (success/fail status set).
        """
        mutations = self._active_mutations.pop(intervention_id, [])
        reversals: list[Mutation] = []

        # Reverse in opposite order
        for m in reversed(mutations):
            if not m.success or not m.is_reversible:
                continue

            adapter = self._adapters.get(m.adapter)
            if adapter is None:
                continue

            reversal = Mutation(
                adapter=m.adapter,
                action=m.reverse_action,  # type: ignore[arg-type]
                timestamp=time.monotonic(),
            )

            try:
                success = await adapter.execute(reversal.action, {})
                reversal.success = success
            except Exception:
                logger.exception(
                    "Error reversing '%s' on adapter '%s'",
                    reversal.action,
                    m.adapter,
                )
                reversal.success = False

            reversals.append(reversal)

        return reversals

    def get_active_mutations(
        self, intervention_id: str
    ) -> list[Mutation]:
        """Get mutations for an active intervention."""
        return self._active_mutations.get(intervention_id, [])

    @property
    def active_intervention_ids(self) -> list[str]:
        """List of intervention IDs with active mutations."""
        return list(self._active_mutations.keys())

    def clear(self) -> None:
        """Clear all tracked mutations."""
        self._active_mutations.clear()
