"""Bounded best-effort writer for non-authority analytics events."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from cortex.libs.schemas.storage import StoredAnalyticsEvent
from cortex.storage.database import SQLiteDatabase, StorageCorruptionError

logger = logging.getLogger(__name__)


class BoundedAnalyticsWriter:
    """Drain a bounded asyncio queue into the shared SQLite connection.

    ``offer`` never awaits and never schedules executor work. Queue saturation
    drops only analytics, increments an observable counter, and cannot block a
    camera/capture producer. Intervention authority and consent never use this
    class; they await critical database transactions directly.
    """

    def __init__(
        self,
        database: SQLiteDatabase,
        *,
        capacity: int = 256,
        batch_size: int = 32,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        if batch_size < 1 or batch_size > capacity:
            raise ValueError("batch_size must be in 1..capacity")
        self._database = database
        self._queue: asyncio.Queue[StoredAnalyticsEvent | None] = asyncio.Queue(maxsize=capacity)
        self._batch_size = batch_size
        self._task: asyncio.Task[None] | None = None
        self._started = False
        self._stopping = False
        self._dropped_total = 0
        self._persisted_total = 0
        self._failed_total = 0

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    @property
    def dropped_total(self) -> int:
        return self._dropped_total

    @property
    def persisted_total(self) -> int:
        return self._persisted_total

    @property
    def failed_total(self) -> int:
        return self._failed_total

    async def start(self) -> None:
        if self._started:
            return
        if self._stopping:
            raise RuntimeError("cannot restart a stopped analytics writer")
        await self._database.start()
        self._started = True
        self._task = asyncio.create_task(
            self._run(),
            name="cortex-sqlite-analytics-writer",
        )

    def offer(self, event: StoredAnalyticsEvent) -> bool:
        """Admit one immutable event without blocking; return acceptance."""

        if self._stopping:
            self._dropped_total += 1
            return False
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._dropped_total += 1
            return False
        return True

    async def _run(self) -> None:
        while True:
            first = await self._queue.get()
            if first is None:
                self._queue.task_done()
                return
            batch = [first]
            while len(batch) < self._batch_size:
                try:
                    item = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if item is None:
                    # Put the stop marker back after this batch so all events
                    # already ahead of it are committed first.
                    self._queue.task_done()
                    await self._queue.put(None)
                    break
                batch.append(item)
            try:
                await self._persist(batch)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._failed_total += len(batch)
                logger.exception(
                    "SQLite analytics batch failed; dropping %d derived events",
                    len(batch),
                )
            finally:
                for _event in batch:
                    self._queue.task_done()

    async def _persist(self, events: list[StoredAnalyticsEvent]) -> None:
        def write(connection: Any) -> int:
            inserted = 0
            for event in events:
                existing = connection.execute(
                    "SELECT payload_sha256 FROM analytics_events WHERE event_id=?",
                    (str(event.event_id),),
                ).fetchone()
                if existing is not None:
                    if str(existing[0]) != event.payload_sha256:
                        raise StorageCorruptionError(
                            f"analytics event id collision: {event.event_id}"
                        )
                    continue
                connection.execute(
                    "INSERT INTO analytics_events("
                    "event_id, event_type, aggregate_type, aggregate_id, "
                    "occurred_at_unix_ms, occurred_at_mono_ns, boot_id, privacy_class, "
                    "payload_json, payload_sha256, expires_at_unix_ms"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(event.event_id),
                        event.event_type,
                        event.aggregate_type,
                        event.aggregate_id,
                        event.occurred_at_unix_ms,
                        event.occurred_at_mono_ns,
                        str(event.boot_id),
                        event.privacy_class,
                        event.payload_json,
                        event.payload_sha256,
                        event.expires_at_unix_ms,
                    ),
                )
                inserted += 1
            return inserted

        self._persisted_total += await self._database.transaction(write)

    async def stop(self, *, timeout_seconds: float = 5.0) -> None:
        """Drain admitted events within a bounded shutdown window."""

        if self._stopping:
            return
        self._stopping = True
        if not self._started:
            while True:
                try:
                    pending = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                self._queue.task_done()
                if pending is not None:
                    self._dropped_total += 1
            return
        try:
            await asyncio.wait_for(self._queue.put(None), timeout=timeout_seconds)
            await asyncio.wait_for(self._queue.join(), timeout=timeout_seconds)
            if self._task is not None:
                await asyncio.wait_for(self._task, timeout=timeout_seconds)
        except TimeoutError:
            if self._task is not None:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
            remaining = self._queue.qsize()
            self._dropped_total += remaining
            logger.warning(
                "SQLite analytics writer shutdown timed out; %d queued events dropped",
                remaining,
            )
        finally:
            self._task = None


__all__ = ["BoundedAnalyticsWriter"]
