"""Tests for focus transition graph and thrashing detection."""
import pytest
from cortex.services.telemetry_engine.focus_graph import FocusGraphBuilder


class TestFocusGraphBuilder:
    def test_high_thrashing_rapid_switches(self):
        """20 switches across 4 apps in 30s -> thrashing > 0.5."""
        builder = FocusGraphBuilder()
        apps = ["Terminal", "Code", "Chrome", "Slack"]
        base_time = 1000.0
        for i in range(20):
            builder.add_event(apps[i % 4], f"Window {i % 4}", base_time + i * 1.5)

        score = builder.compute_thrashing_score(current_time=base_time + 30)
        assert score > 0.5

    def test_low_thrashing_single_app(self):
        """Steady single-app usage with different windows -> low thrashing."""
        builder = FocusGraphBuilder()
        base_time = 1000.0
        for i in range(5):
            builder.add_event("Code", f"file_{i}.ts", base_time + i * 15)

        score = builder.compute_thrashing_score(current_time=base_time + 75)
        assert score < 0.35

    def test_no_events_zero_score(self):
        """Empty graph should return 0.0 thrashing score."""
        builder = FocusGraphBuilder()
        assert builder.compute_thrashing_score() == 0.0

    def test_single_event_zero_score(self):
        """Single event (no switch) should return 0.0."""
        builder = FocusGraphBuilder()
        builder.add_event("Code", "file.ts", 100.0)
        assert builder.compute_thrashing_score(current_time=102.0) == 0.0

    def test_same_node_ignored(self):
        """Duplicate consecutive events for same node are ignored."""
        builder = FocusGraphBuilder()
        builder.add_event("Code", "file.ts", 100.0)
        builder.add_event("Code", "file.ts", 101.0)  # same node
        assert builder.compute_thrashing_score(current_time=102.0) == 0.0

    def test_two_nodes_below_min_threshold(self):
        """Switching between only 2 nodes should not trigger thrashing (needs >= 3)."""
        builder = FocusGraphBuilder()
        base_time = 1000.0
        for i in range(10):
            app = "Code" if i % 2 == 0 else "Terminal"
            builder.add_event(app, f"w{i % 2}", base_time + i * 2)

        score = builder.compute_thrashing_score(current_time=base_time + 20)
        assert score == 0.0

    def test_alignment_score_on_task(self):
        """Windows matching goal keywords should produce high alignment."""
        builder = FocusGraphBuilder()
        builder.add_event("Code", "auth.ts - VS Code", 100.0)
        builder.add_event("Chrome", "OAuth docs", 110.0)
        builder.add_event("Code", "auth.ts - VS Code", 120.0)

        score = builder.get_alignment_score(["auth", "oauth"], current_time=130.0)
        assert score > 0.5

    def test_alignment_score_off_task(self):
        """Windows not matching goal keywords should produce low alignment."""
        builder = FocusGraphBuilder()
        builder.add_event("Chrome", "Reddit - funny", 100.0)
        builder.add_event("Chrome", "YouTube - cats", 110.0)

        score = builder.get_alignment_score(["auth", "oauth"], current_time=120.0)
        assert score < 0.3

    def test_alignment_no_goal_keywords(self):
        """Empty goal keywords should return 1.0 (assume aligned)."""
        builder = FocusGraphBuilder()
        builder.add_event("Code", "file.ts", 100.0)
        builder.add_event("Chrome", "docs", 110.0)

        score = builder.get_alignment_score([], current_time=120.0)
        assert score == 1.0

    def test_alignment_no_events(self):
        """No events should return 1.0."""
        builder = FocusGraphBuilder()
        score = builder.get_alignment_score(["auth"], current_time=120.0)
        assert score == 1.0

    def test_get_top_nodes(self):
        """get_top_nodes should return most-visited nodes up to n."""
        builder = FocusGraphBuilder()
        for i in range(10):
            builder.add_event("Code" if i % 2 == 0 else "Terminal", f"w{i}", 100.0 + i)

        nodes = builder.get_top_nodes(n=3, current_time=110.0)
        assert len(nodes) <= 3
        # Each node dict should have required keys
        for node in nodes:
            assert "node_id" in node
            assert "app_name" in node
            assert "visit_count" in node

    def test_get_top_nodes_empty(self):
        """get_top_nodes with no events should return empty list."""
        builder = FocusGraphBuilder()
        nodes = builder.get_top_nodes(n=3, current_time=110.0)
        assert nodes == []

    def test_get_recent_transitions(self):
        """get_recent_transitions should return correct number of transitions."""
        builder = FocusGraphBuilder()
        builder.add_event("A", "w1", 100.0)
        builder.add_event("B", "w2", 105.0)
        builder.add_event("C", "w3", 110.0)

        transitions = builder.get_recent_transitions(n=5, current_time=115.0)
        assert len(transitions) == 2
        # Each transition should have from, to, dwell_seconds
        for t in transitions:
            assert "from" in t
            assert "to" in t
            assert "dwell_seconds" in t

    def test_get_recent_transitions_empty(self):
        """No events should return no transitions."""
        builder = FocusGraphBuilder()
        transitions = builder.get_recent_transitions(n=5, current_time=115.0)
        assert transitions == []

    def test_clear(self):
        """clear() should reset all state."""
        builder = FocusGraphBuilder()
        builder.add_event("A", "w1", 100.0)
        builder.add_event("B", "w2", 105.0)
        builder.add_event("C", "w3", 110.0)
        builder.clear()
        assert builder.compute_thrashing_score(current_time=115.0) == 0.0

    def test_old_events_outside_window(self):
        """Events outside the analysis window should not affect score."""
        builder = FocusGraphBuilder(window_seconds=60.0)
        # Add events far in the past
        builder.add_event("A", "w1", 0.0)
        builder.add_event("B", "w2", 1.0)
        builder.add_event("C", "w3", 2.0)
        builder.add_event("D", "w4", 3.0)

        # Query at a time well past the window
        score = builder.compute_thrashing_score(current_time=200.0)
        assert score == 0.0

    def test_max_events_trimming(self):
        """Adding more than max_events should trim old ones."""
        builder = FocusGraphBuilder(max_events=10)
        for i in range(20):
            builder.add_event(f"App{i}", f"w{i}", 100.0 + i)

        # Internal events list should be trimmed
        assert len(builder._events) <= 10
