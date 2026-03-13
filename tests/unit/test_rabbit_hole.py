"""Tests for anti-rabbit-hole circuit breaker."""
import pytest
from cortex.services.state_engine.rabbit_hole import RabbitHoleDetector, RabbitHoleAlert


class TestRabbitHoleDetector:
    def test_fires_when_off_task_exceeds_drift_threshold(self):
        """Goal 'A* search', 15 min in ui_animations.ts -> fires alert."""
        detector = RabbitHoleDetector(
            min_drift_minutes=10.0,
            alignment_threshold=0.3,
            cooldown_seconds=600.0,
        )
        base_time = 1000.0

        # Simulate being in ui_animations.ts for 15+ minutes
        # First check starts drift tracking
        alert = detector.check(
            goal="implement A* search",
            current_file="ui_animations.ts",
            current_app="Code",
            tab_titles=["ui_animations.ts - VS Code"],
            state="FLOW",
            current_time=base_time,
        )
        assert alert is None  # drift just started

        # 15 minutes later, still in the wrong file
        alert = detector.check(
            goal="implement A* search",
            current_file="ui_animations.ts",
            current_app="Code",
            tab_titles=["ui_animations.ts - VS Code"],
            state="FLOW",
            current_time=base_time + 15 * 60,
        )
        assert alert is not None
        assert isinstance(alert, RabbitHoleAlert)
        assert alert.goal == "implement A* search"
        assert alert.current_file == "ui_animations.ts"
        assert alert.drift_minutes >= 10.0
        assert alert.alignment_score < 0.3

    def test_no_fire_when_on_task(self):
        """Working on files matching the goal should not trigger alert."""
        detector = RabbitHoleDetector(
            min_drift_minutes=10.0,
            alignment_threshold=0.3,
            cooldown_seconds=600.0,
        )
        base_time = 1000.0

        for i in range(20):
            alert = detector.check(
                goal="implement A* search",
                current_file="search_algorithm.ts",
                current_app="Code",
                tab_titles=["search_algorithm.ts", "A* pathfinding docs"],
                state="FLOW",
                current_time=base_time + i * 60,
            )
            assert alert is None

    def test_keyword_matching_in_file_name(self):
        """File name containing goal keywords should count as on-task."""
        detector = RabbitHoleDetector(
            min_drift_minutes=10.0,
            alignment_threshold=0.3,
        )
        # "search" keyword matches
        alert = detector.check(
            goal="implement A* search",
            current_file="astar_search.py",
            current_app="Code",
            state="FLOW",
            current_time=1000.0,
        )
        assert alert is None
        assert not detector.is_drifting

    def test_reset_after_firing(self):
        """After an alert fires, drift state should reset."""
        detector = RabbitHoleDetector(
            min_drift_minutes=10.0,
            alignment_threshold=0.3,
            cooldown_seconds=600.0,
        )
        base_time = 1000.0

        # Start drift
        detector.check(
            goal="implement A* search",
            current_file="ui_animations.ts",
            current_app="Code",
            state="FLOW",
            current_time=base_time,
        )

        # Fire after 15 minutes
        alert = detector.check(
            goal="implement A* search",
            current_file="ui_animations.ts",
            current_app="Code",
            state="FLOW",
            current_time=base_time + 15 * 60,
        )
        assert alert is not None

        # After firing, drift_start should be reset
        assert not detector.is_drifting

    def test_cooldown_prevents_rapid_refire(self):
        """After firing, cooldown should prevent re-firing."""
        cooldown = 600.0
        detector = RabbitHoleDetector(
            min_drift_minutes=10.0,
            alignment_threshold=0.3,
            cooldown_seconds=cooldown,
        )
        base_time = 1000.0

        # First fire
        detector.check(
            goal="implement A* search",
            current_file="ui_animations.ts",
            current_app="Code",
            state="FLOW",
            current_time=base_time,
        )
        alert1 = detector.check(
            goal="implement A* search",
            current_file="ui_animations.ts",
            current_app="Code",
            state="FLOW",
            current_time=base_time + 15 * 60,
        )
        assert alert1 is not None

        # Try immediately again -- should be blocked by cooldown
        fire_time = base_time + 15 * 60
        detector.check(
            goal="implement A* search",
            current_file="ui_animations.ts",
            current_app="Code",
            state="FLOW",
            current_time=fire_time + 1,
        )
        alert2 = detector.check(
            goal="implement A* search",
            current_file="ui_animations.ts",
            current_app="Code",
            state="FLOW",
            current_time=fire_time + 15 * 60,
        )
        assert alert2 is None  # within cooldown

    def test_fires_after_cooldown_expires(self):
        """After cooldown expires, should be able to fire again."""
        cooldown = 600.0
        detector = RabbitHoleDetector(
            min_drift_minutes=10.0,
            alignment_threshold=0.3,
            cooldown_seconds=cooldown,
        )
        base_time = 1000.0

        # First fire
        detector.check(
            goal="implement A* search",
            current_file="ui_animations.ts",
            current_app="Code",
            state="FLOW",
            current_time=base_time,
        )
        alert1 = detector.check(
            goal="implement A* search",
            current_file="ui_animations.ts",
            current_app="Code",
            state="FLOW",
            current_time=base_time + 15 * 60,
        )
        assert alert1 is not None

        # Wait past cooldown, start new drift
        post_cooldown = base_time + 15 * 60 + cooldown + 1
        detector.check(
            goal="implement A* search",
            current_file="ui_animations.ts",
            current_app="Code",
            state="FLOW",
            current_time=post_cooldown,
        )
        alert2 = detector.check(
            goal="implement A* search",
            current_file="ui_animations.ts",
            current_app="Code",
            state="FLOW",
            current_time=post_cooldown + 15 * 60,
        )
        assert alert2 is not None

    def test_no_fire_without_goal(self):
        """Empty goal should never fire."""
        detector = RabbitHoleDetector(min_drift_minutes=10.0)
        alert = detector.check(
            goal="",
            current_file="random.ts",
            current_app="Code",
            state="FLOW",
            current_time=1000.0,
        )
        assert alert is None

    def test_no_fire_in_hyper_state(self):
        """HYPER state should not trigger rabbit hole detection."""
        detector = RabbitHoleDetector(
            min_drift_minutes=10.0,
            alignment_threshold=0.3,
        )
        base_time = 1000.0

        detector.check(
            goal="implement A* search",
            current_file="ui_animations.ts",
            current_app="Code",
            state="HYPER",
            current_time=base_time,
        )
        alert = detector.check(
            goal="implement A* search",
            current_file="ui_animations.ts",
            current_app="Code",
            state="HYPER",
            current_time=base_time + 15 * 60,
        )
        assert alert is None

    def test_fires_in_hypo_state(self):
        """HYPO state should allow rabbit hole detection (same as FLOW)."""
        detector = RabbitHoleDetector(
            min_drift_minutes=10.0,
            alignment_threshold=0.3,
            cooldown_seconds=600.0,
        )
        base_time = 1000.0

        detector.check(
            goal="implement A* search",
            current_file="ui_animations.ts",
            current_app="Code",
            state="HYPO",
            current_time=base_time,
        )
        alert = detector.check(
            goal="implement A* search",
            current_file="ui_animations.ts",
            current_app="Code",
            state="HYPO",
            current_time=base_time + 15 * 60,
        )
        assert alert is not None

    def test_drift_resets_when_back_on_task(self):
        """Switching back to on-task file should reset drift timer."""
        detector = RabbitHoleDetector(
            min_drift_minutes=10.0,
            alignment_threshold=0.3,
        )
        base_time = 1000.0

        # Start drifting
        detector.check(
            goal="implement A* search",
            current_file="ui_animations.ts",
            current_app="Code",
            state="FLOW",
            current_time=base_time,
        )
        assert detector.is_drifting

        # Return to on-task file
        detector.check(
            goal="implement A* search",
            current_file="search_algorithm.ts",
            current_app="Code",
            tab_titles=["search docs"],
            state="FLOW",
            current_time=base_time + 5 * 60,
        )
        assert not detector.is_drifting

    def test_suggested_file_from_on_task_history(self):
        """Alert should suggest a previously recorded on-task file."""
        detector = RabbitHoleDetector(
            min_drift_minutes=10.0,
            alignment_threshold=0.3,
            cooldown_seconds=600.0,
        )
        base_time = 1000.0

        # First, work on-task to record a goal file
        detector.check(
            goal="implement A* search",
            current_file="search.ts",
            current_app="Code",
            tab_titles=["search docs"],
            state="FLOW",
            current_time=base_time,
        )

        # Then drift off-task
        detector.check(
            goal="implement A* search",
            current_file="ui_animations.ts",
            current_app="Code",
            state="FLOW",
            current_time=base_time + 60,
        )
        alert = detector.check(
            goal="implement A* search",
            current_file="ui_animations.ts",
            current_app="Code",
            state="FLOW",
            current_time=base_time + 60 + 15 * 60,
        )
        assert alert is not None
        assert alert.suggested_file == "search.ts"

    def test_set_goal_extracts_keywords(self):
        """set_goal should extract meaningful keywords, filtering stop words."""
        detector = RabbitHoleDetector()
        detector.set_goal("implement the A* search algorithm")
        # "implement", "the", "a" are stop words; "search", "algorithm" should remain
        assert "search" in detector._goal_keywords
        assert "algorithm" in detector._goal_keywords
        assert "the" not in detector._goal_keywords
        assert "implement" not in detector._goal_keywords

    def test_alert_summary_contains_useful_info(self):
        """Alert summary should mention current file and goal."""
        detector = RabbitHoleDetector(
            min_drift_minutes=10.0,
            alignment_threshold=0.3,
            cooldown_seconds=600.0,
        )
        base_time = 1000.0

        detector.check(
            goal="implement A* search",
            current_file="ui_animations.ts",
            current_app="Code",
            state="FLOW",
            current_time=base_time,
        )
        alert = detector.check(
            goal="implement A* search",
            current_file="ui_animations.ts",
            current_app="Code",
            state="FLOW",
            current_time=base_time + 15 * 60,
        )
        assert alert is not None
        assert "ui_animations.ts" in alert.summary
        assert "A* search" in alert.summary
