"""
Activity Aggregator

Receives ACTIVITY_SYNC messages from the browser extension, stores daily
activity timelines, and provides query methods for the handover/briefing system.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from cortex.libs.schemas.activity import ActivitySummary, ActivityTimeline

logger = logging.getLogger(__name__)

# TTL for daily timelines: 90 days
_TIMELINE_TTL = 90 * 86400


class _JsonStore(Protocol):
    """Structural interface for the JSON-blob store the aggregator needs.

    Both ``InMemoryStore`` and ``RedisStore`` satisfy this; typing the
    dependency as a Protocol (rather than bare ``object``) lets mypy see
    the ``get_json`` / ``set_json`` methods the aggregator calls.
    """

    async def get_json(self, key: str) -> dict[str, Any] | None: ...

    async def set_json(
        self, key: str, value: dict[str, Any], ttl_seconds: int | None = ...,
    ) -> None: ...


class ActivityAggregator:
    """Aggregates browser activity syncs into daily timelines."""

    def __init__(self, store: _JsonStore) -> None:
        """
        Args:
            store: A store instance with get_json/set_json methods
                   (InMemoryStore or RedisStore).
        """
        self._store = store
        # B15 (Phase 4.1): operator-facing counter of activity records
        # that failed Pydantic validation and were silently skipped.
        # A nonzero value indicates the browser extension is sending
        # a schema-mismatched payload; surfaced via ``get_summary``.
        self._malformed_records: int = 0

    @property
    def malformed_records(self) -> int:
        """B15 (Phase 4.1): cumulative count of malformed activity records."""
        return self._malformed_records

    def get_summary(self) -> dict[str, int]:
        """B15 (Phase 4.1): operator-facing diagnostics snapshot."""
        return {"malformed_records": self._malformed_records}

    async def ingest(self, activities: list[dict[str, Any]]) -> None:
        """Ingest a batch of activity summaries from the browser extension.

        Each activity is upserted into today's timeline. Duplicate content_ids
        are merged (latest wins).
        """
        if not activities:
            return

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        key = f"activity:timeline:{today}"

        existing = await self._store.get_json(key)
        timeline = ActivityTimeline(**existing) if existing else ActivityTimeline(date=today)

        # Index existing activities by content_id for dedup/merge
        by_id = {a.content_id: a for a in timeline.activities}

        for raw in activities:
            try:
                summary = ActivitySummary(**raw)
            except Exception:
                # B15 (Phase 4.1): bump the counter so a misbehaving
                # extension is observable via ``get_summary``.
                self._malformed_records += 1
                logger.debug("Skipping malformed activity: %s", raw)
                continue
            by_id[summary.content_id] = summary

        timeline.activities = list(by_id.values())

        # Recalculate aggregates
        timeline.total_learning_s = sum(a.duration_spent_s for a in timeline.activities)
        all_tags: list[str] = []
        for a in timeline.activities:
            all_tags.extend(a.topic_tags)
        timeline.dominant_topics = [
            tag for tag, _ in Counter(all_tags).most_common(5)
        ]

        await self._store.set_json(key, timeline.model_dump(), ttl_seconds=_TIMELINE_TTL)

    async def get_timeline(self, date: str) -> ActivityTimeline | None:
        """Retrieve the activity timeline for a given date (YYYY-MM-DD)."""
        key = f"activity:timeline:{date}"
        data = await self._store.get_json(key)
        if data:
            return ActivityTimeline(**data)
        return None

    async def get_recent_activities(self, limit: int = 5) -> list[ActivitySummary]:
        """Get the most recent activities across all stored timelines.

        Searches today and yesterday's timelines, returns up to `limit` items
        sorted by last_visited descending.
        """
        now = datetime.now(UTC)
        activities: list[ActivitySummary] = []

        for days_ago in range(3):
            date_str = (now - timedelta(days=days_ago)).strftime("%Y-%m-%d")
            tl = await self.get_timeline(date_str)
            if tl:
                activities.extend(tl.activities)

        # Deduplicate by content_id, keeping latest
        seen: dict[str, ActivitySummary] = {}
        for a in activities:
            if a.content_id not in seen or a.last_visited > seen[a.content_id].last_visited:
                seen[a.content_id] = a

        result = sorted(seen.values(), key=lambda a: a.last_visited, reverse=True)
        return result[:limit]
