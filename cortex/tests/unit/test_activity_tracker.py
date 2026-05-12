"""Tests for the Activity Tracker aggregator and schemas."""

from __future__ import annotations

from datetime import UTC

import pytest

from cortex.libs.schemas.activity import ActivitySummary, ActivityTimeline
from cortex.libs.store.memory_store import InMemoryStore
from cortex.services.activity_tracker.aggregator import ActivityAggregator
from cortex.services.activity_tracker.summarizer import ActivitySummarizer

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store():
    return InMemoryStore()


@pytest.fixture
def aggregator(store):
    return ActivityAggregator(store=store)


@pytest.fixture
def summarizer(store):
    return ActivitySummarizer(store=store)


def _make_activity(
    content_id: str = "https://youtube.com/watch?v=abc123",
    platform: str = "youtube",
    title: str = "Test Video",
    duration_spent_s: float = 300,
    completion_pct: float = 43.0,
    last_visited: float = 1710000000000,
    **kwargs,
) -> dict:
    return {
        "content_id": content_id,
        "platform": platform,
        "content_type": kwargs.get("content_type", "video"),
        "title": title,
        "url": kwargs.get("url", content_id),
        "position_description": kwargs.get("position_description", "32:48 / 1:15:22"),
        "duration_spent_s": duration_spent_s,
        "last_visited": last_visited,
        "completion_pct": completion_pct,
        "topic_tags": kwargs.get("topic_tags", ["algorithm"]),
        "context_snapshot": kwargs.get("context_snapshot", "Test content snapshot"),
    }


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

class TestActivitySchemas:
    def test_activity_summary_valid(self):
        summary = ActivitySummary(**_make_activity())
        assert summary.content_id == "https://youtube.com/watch?v=abc123"
        assert summary.platform == "youtube"
        assert summary.completion_pct == 43.0

    def test_activity_timeline_valid(self):
        timeline = ActivityTimeline(
            date="2026-03-14",
            activities=[ActivitySummary(**_make_activity())],
            total_learning_s=300,
            dominant_topics=["algorithm"],
        )
        assert timeline.date == "2026-03-14"
        assert len(timeline.activities) == 1
        assert timeline.total_learning_s == 300

    def test_activity_summary_defaults(self):
        summary = ActivitySummary(
            content_id="test", platform="test", content_type="article",
            title="Test", url="https://test.com", last_visited=1710000000000,
        )
        assert summary.duration_spent_s == 0
        assert summary.completion_pct == 0
        assert summary.topic_tags == []


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------

class TestActivityAggregator:
    @pytest.mark.asyncio
    async def test_ingest_single_activity(self, aggregator):
        activities = [_make_activity()]
        await aggregator.ingest(activities)

        from datetime import datetime
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        timeline = await aggregator.get_timeline(today)

        assert timeline is not None
        assert len(timeline.activities) == 1
        assert timeline.activities[0].title == "Test Video"
        assert timeline.total_learning_s == 300

    @pytest.mark.asyncio
    async def test_ingest_deduplicates_by_content_id(self, aggregator):
        act1 = _make_activity(title="Video v1", duration_spent_s=100)
        act2 = _make_activity(title="Video v2", duration_spent_s=200)

        await aggregator.ingest([act1])
        await aggregator.ingest([act2])

        from datetime import datetime
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        timeline = await aggregator.get_timeline(today)

        assert timeline is not None
        assert len(timeline.activities) == 1
        # Latest wins
        assert timeline.activities[0].title == "Video v2"

    @pytest.mark.asyncio
    async def test_ingest_multiple_activities(self, aggregator):
        activities = [
            _make_activity(content_id="https://youtube.com/watch?v=a", title="Video A"),
            _make_activity(content_id="https://leetcode.com/problems/two-sum", title="Two Sum",
                           platform="leetcode", content_type="code_problem"),
        ]
        await aggregator.ingest(activities)

        from datetime import datetime
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        timeline = await aggregator.get_timeline(today)

        assert timeline is not None
        assert len(timeline.activities) == 2
        assert timeline.total_learning_s == 600

    @pytest.mark.asyncio
    async def test_dominant_topics_calculated(self, aggregator):
        activities = [
            _make_activity(content_id="a", topic_tags=["algorithm", "python"]),
            _make_activity(content_id="b", topic_tags=["algorithm", "data structure"]),
        ]
        await aggregator.ingest(activities)

        from datetime import datetime
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        timeline = await aggregator.get_timeline(today)

        assert "algorithm" in timeline.dominant_topics

    @pytest.mark.asyncio
    async def test_get_timeline_returns_none_for_missing_date(self, aggregator):
        timeline = await aggregator.get_timeline("2020-01-01")
        assert timeline is None

    @pytest.mark.asyncio
    async def test_get_recent_activities(self, aggregator):
        activities = [
            _make_activity(content_id="a", last_visited=1710000000000),
            _make_activity(content_id="b", last_visited=1710000001000),
            _make_activity(content_id="c", last_visited=1710000002000),
        ]
        await aggregator.ingest(activities)

        recent = await aggregator.get_recent_activities(limit=2)
        assert len(recent) == 2
        # Should be sorted by last_visited descending
        assert recent[0].content_id == "c"
        assert recent[1].content_id == "b"

    @pytest.mark.asyncio
    async def test_skips_malformed_activities(self, aggregator):
        activities = [
            {"content_id": "valid", "platform": "test", "content_type": "article",
             "title": "Valid", "url": "https://test.com", "last_visited": 1710000000000},
            {"bad_field": True},  # malformed
        ]
        await aggregator.ingest(activities)

        from datetime import datetime
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        timeline = await aggregator.get_timeline(today)
        assert len(timeline.activities) == 1


# ---------------------------------------------------------------------------
# Summarizer
# ---------------------------------------------------------------------------

class TestActivitySummarizer:
    @pytest.mark.asyncio
    async def test_template_recap_no_llm(self, summarizer):
        activity = ActivitySummary(**_make_activity())
        recap = await summarizer.get_recap(activity)

        assert "Test Video" in recap
        assert "youtube" in recap

    @pytest.mark.asyncio
    async def test_recap_is_cached(self, summarizer, store):
        activity = ActivitySummary(**_make_activity())

        recap1 = await summarizer.get_recap(activity)
        recap2 = await summarizer.get_recap(activity)

        assert recap1 == recap2

    @pytest.mark.asyncio
    async def test_template_includes_position(self, summarizer):
        activity = ActivitySummary(
            **_make_activity(position_description="32:48 / 1:15:22")
        )
        recap = await summarizer.get_recap(activity)
        assert "32:48" in recap
