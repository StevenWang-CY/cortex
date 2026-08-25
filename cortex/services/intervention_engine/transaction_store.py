"""Crash-safe persistence adapters for intervention transactions."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from cortex.libs.schemas.intervention_transaction import (
    InterventionTransactionJournal,
)
from cortex.libs.utils.atomic_write import atomic_write_json
from cortex.storage.intervention_store import SQLiteInterventionTransactionStore

logger = logging.getLogger(__name__)


class InMemoryInterventionTransactionStore:
    """Deterministic store for tests and composition without a data path."""

    def __init__(
        self,
        initial: InterventionTransactionJournal | None = None,
    ) -> None:
        self._journal = (initial or InterventionTransactionJournal()).model_copy(
            deep=True,
        )
        self._lock = asyncio.Lock()

    async def load(self) -> InterventionTransactionJournal:
        async with self._lock:
            return self._journal.model_copy(deep=True)

    async def save(self, journal: InterventionTransactionJournal) -> None:
        validated = InterventionTransactionJournal.model_validate(
            journal.model_dump(mode="json")
        )
        async with self._lock:
            self._journal = validated.model_copy(deep=True)


class JsonInterventionTransactionStore:
    """Atomic JSON journal used until the WP7 SQLite migration lands.

    Invalid journals are retained as ``*.corrupt.<pid>.<nonce>`` rather than
    deleted. The nonce prevents a second corruption in the same process from
    overwriting the first forensic copy.
    This keeps startup fail-safe (no stale authorization is revived) while
    preserving the bytes for diagnosis or manual recovery.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def _load_sync(self) -> InterventionTransactionJournal:
        if not self._path.exists():
            return InterventionTransactionJournal()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            return InterventionTransactionJournal.model_validate(raw)
        except (OSError, json.JSONDecodeError, ValidationError, TypeError):
            logger.exception(
                "Invalid intervention transaction journal at %s; "
                "quarantining and starting with no authority",
                self._path,
            )
            quarantine = self._path.with_name(
                f"{self._path.name}.corrupt.{os.getpid()}.{uuid4().hex}"
            )
            try:
                os.replace(self._path, quarantine)
            except OSError:
                logger.exception(
                    "Could not quarantine invalid transaction journal %s",
                    self._path,
                )
            return InterventionTransactionJournal()

    async def load(self) -> InterventionTransactionJournal:
        async with self._lock:
            journal = await asyncio.to_thread(self._load_sync)
            return journal.model_copy(deep=True)

    async def save(self, journal: InterventionTransactionJournal) -> None:
        snapshot = copy.deepcopy(journal.model_dump(mode="json"))
        validated = InterventionTransactionJournal.model_validate(snapshot)
        async with self._lock:
            await asyncio.to_thread(
                atomic_write_json,
                self._path,
                validated.model_dump(mode="json"),
            )


__all__ = [
    "InMemoryInterventionTransactionStore",
    "JsonInterventionTransactionStore",
    "SQLiteInterventionTransactionStore",
]
