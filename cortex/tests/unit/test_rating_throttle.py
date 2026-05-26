"""P0 §3.8: helpfulness rating throttle + text feedback unit tests."""

from __future__ import annotations

import time

import pytest

from cortex.services.eval.helpfulness import HelpfulnessTracker


def _start(tracker: HelpfulnessTracker, intervention_id: str = "int_test") -> None:
    tracker.start_tracking(
        intervention_id=intervention_id,
        intervention_type="overlay_only",
        state="HYPER",
        confidence=0.9,
    )


def test_record_rating_with_text_feedback() -> None:
    tracker = HelpfulnessTracker()
    _start(tracker)
    tracker.record_rating(
        "int_test",
        "thumbs_down",
        text_feedback="It was too soon, tabs were fine.",
    )
    # Internal state — confirm the text landed on the tracked record.
    record = tracker._active["int_test"]
    assert record.user_rating == "thumbs_down"
    assert record.text_feedback == "It was too soon, tabs were fine."


def test_record_rating_clips_text_feedback_to_200_chars() -> None:
    tracker = HelpfulnessTracker()
    _start(tracker)
    long_text = "x" * 500
    tracker.record_rating("int_test", "thumbs_down", text_feedback=long_text)
    record = tracker._active["int_test"]
    assert record.text_feedback is not None
    assert len(record.text_feedback) == 200


def test_record_rating_ignores_invalid_rating() -> None:
    tracker = HelpfulnessTracker()
    _start(tracker)
    tracker.record_rating("int_test", "thumbs_sideways")
    record = tracker._active["int_test"]
    assert record.user_rating is None


def test_downvote_count_within_window_returns_recent_only() -> None:
    tracker = HelpfulnessTracker()
    _start(tracker)
    # Five downvotes within the last "window".
    for _ in range(5):
        tracker.record_rating("int_test", "thumbs_down")
    assert tracker.downvote_count_within(30.0) == 5
    # Let real time elapse, then re-check with a shorter window.
    time.sleep(0.05)
    assert tracker.downvote_count_within(0.01) == 0  # all stale


def test_reset_downvote_window_clears_counter() -> None:
    tracker = HelpfulnessTracker()
    _start(tracker)
    for _ in range(3):
        tracker.record_rating("int_test", "thumbs_down")
    assert tracker.downvote_count_within(30.0) == 3
    tracker.reset_downvote_window()
    assert tracker.downvote_count_within(30.0) == 0


@pytest.mark.asyncio
async def test_end_tracking_carries_text_feedback_into_record() -> None:
    tracker = HelpfulnessTracker()
    _start(tracker)
    tracker.record_rating(
        "int_test", "thumbs_down", text_feedback="needed quiet mode",
    )
    rec = await tracker.end_tracking(
        intervention_id="int_test",
        state="FLOW",
        confidence=0.9,
    )
    assert rec is not None
    assert rec["user_rating"] == "thumbs_down"
    assert rec["text_feedback"] == "needed quiet mode"
