"""Persistence boundary for transactional intervention state."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from cortex.libs.schemas.intervention_transaction import (
    InterventionTransactionJournal,
)


@runtime_checkable
class InterventionTransactionStore(Protocol):
    """Crash-safe whole-journal persistence used by the WP6 coordinator.

    WP7 replaces the JSON implementation with SQLite without changing the
    domain service.  Implementations must return independent model instances;
    callers are allowed to mutate the returned aggregate under their own lock.
    """

    async def load(self) -> InterventionTransactionJournal:
        """Load the most recent valid journal or an empty journal."""
        ...

    async def save(self, journal: InterventionTransactionJournal) -> None:
        """Durably replace the stored journal."""
        ...


__all__ = ["InterventionTransactionStore"]
