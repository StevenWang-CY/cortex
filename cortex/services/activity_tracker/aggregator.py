"""
Activity Aggregator

Receives ACTIVITY_SYNC messages from the browser extension, stores daily
activity timelines, and provides query methods for the handover/briefing system.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

from cortex.libs.schemas.activity import ActivitySummary, ActivityTimeline

logger = logging.getLogger(__name__)

# TTL for daily timelines: 90 days
_TIMELINE_TTL = 90 * 86400


class ActivityAggregator:
    """Aggregates browser activity syncs into daily timelines."""

    def __init__(self, store: object) -> None:
        """
        Args:
            store: A store instance with get_json/set_json methods
                   (InMemoryStore or RedisStore).
        """
        self._store = store

    async def ingest(self, activities: list[dict]) -> None:
        """Ingest a batch of activity summaries from the browser extension.

        Each activity is upserted into today's timeline. Duplicate content_ids
        are merged (latest wins).
        """
        if not activities:
            return

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = f"activity:timeline:{today}"

        existing = await self._store.get_json(key)
        timeline = ActivityTimeline(**existing) if existing else ActivityTimeline(date=today)

        # Index existing activities by content_id for dedup/merge
        by_id = {a.content_id: a for a in timeline.activities}

        for raw in activities:
            try:
                summary = ActivitySummary(**raw)
            except Exception:
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
        now = datetime.now(timezone.utc)
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
