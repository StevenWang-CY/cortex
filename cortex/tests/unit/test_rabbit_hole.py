"""Tests for RabbitHoleDetector — anti-rabbit hole circuit breaker."""
import pytest
from cortex.services.state_engine.rabbit_hole import RabbitHoleDetector, RabbitHoleAlert


# Use base_t > default cooldown (600s) so initial _last_trigger=0.0 doesn't block
_BASE_T = 1000.0


class TestRabbitHoleDetector:
    def test_no_goal_no_alert(self):
        detector = RabbitHoleDetector()
        result = detector.check(
            goal="", current_file="random.ts", current_app="Code", current_time=_BASE_T,
        )
        assert result is None

    def test_on_task_no_alert(self):
        """Working on goal-aligned files → no alert."""
        detector = RabbitHoleDetector(min_drift_minutes=1.0)
        for i in range(200):
            result = detector.check(
                goal="implement search algorithm",
                current_file="search_algorithm.py",
                current_app="Code",
                current_time=_BASE_T + float(i),
            )
            assert result is None

    def test_fires_after_min_drift(self):
        """15 min in unrelated file → fires."""
        detector = RabbitHoleDetector(min_drift_minutes=10.0, cooldown_seconds=0.0)
        alert = None
        for i in range(800):
            result = detector.check(
                goal="implement search algorithm",
                current_file="ui_animations.ts",
                current_app="Code",
                state="FLOW",
                current_time=_BASE_T + float(i),
            )
            if result is not None:
                alert = result
                break
        assert alert is not None
        assert isinstance(alert, RabbitHoleAlert)
        assert alert.drift_minutes >= 10.0
        assert "ui_animations" in alert.summary

    def test_hyper_state_no_trigger(self):
        """In HYPER state, don't trigger (user already struggling)."""
        detector = RabbitHoleDetector(min_drift_minutes=0.1, cooldown_seconds=0.0)
        for i in range(100):
            result = detector.check(
                goal="implement search algorithm",
                current_file="unrelated.ts",
                current_app="Code",
                state="HYPER",
                current_time=_BASE_T + float(i),
            )
            assert result is None

    def test_cooldown_prevents_rapid_retrigger(self):
        detector = RabbitHoleDetector(min_drift_minutes=0.1, cooldown_seconds=600.0)
        # First trigger — start well past initial cooldown
        triggered = False
        for i in range(100):
            result = detector.check(
                goal="search", current_file="unrelated.ts", current_app="Code",
                state="FLOW", current_time=_BASE_T + float(i),
            )
            if result is not None:
                triggered = True
                trigger_time = _BASE_T + float(i)
                break
        assert triggered
        # Immediately after → cooldown
        result = detector.check(
            goal="search", current_file="unrelated.ts", current_app="Code",
            state="FLOW", current_time=trigger_time + 1.0,
        )
        assert result is None

    def test_keyword_extraction_filters_stop_words(self):
        detector = RabbitHoleDetector()
        detector.set_goal("implement the core A* search algorithm")
        # "implement", "the", "core" are stop words
        assert "search" in detector._goal_keywords
        assert "the" not in detector._goal_keywords

    def test_suggested_file_from_on_task_history(self):
        detector = RabbitHoleDetector(min_drift_minutes=0.1, cooldown_seconds=0.0)
        # First work on-task
        detector.check(
            goal="search algorithm",
            current_file="search.py",
            current_app="Code",
            state="FLOW",
            current_time=_BASE_T,
        )
        # Then drift
        alert = None
        for i in range(1, 200):
            result = detector.check(
                goal="search algorithm",
                current_file="ui_animations.ts",
                current_app="Code",
                state="FLOW",
                current_time=_BASE_T + float(i),
            )
            if result is not None:
                alert = result
                break
        if alert is not None:
            assert alert.suggested_file == "search.py"

    def test_is_drifting_property(self):
        detector = RabbitHoleDetector(cooldown_seconds=0.0)
        assert not detector.is_drifting
        detector.check(
            goal="search", current_file="unrelated.ts", current_app="Code",
            state="FLOW", current_time=_BASE_T,
        )
        assert detector.is_drifting
