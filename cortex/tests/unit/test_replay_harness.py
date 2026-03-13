"""Tests for the Replay Harness."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from cortex.scripts.replay_harness import ReplayHarness


def _generate_events(count: int = 50) -> list[dict]:
    """
    Generate a synthetic session with *count* events spread across
    state, intervention_plan, intervention_outcome, and helpfulness types.
    """
    events = []
    t = 1000.0
    intervention_ids = []

    for i in range(count):
        t += 1.0
        remainder = i % 5

        if remainder == 0:
            # state event
            events.append({
                "type": "state",
                "timestamp": t,
                "payload": {
                    "state": ["FLOW", "HYPER", "HYPO", "RECOVERY"][i % 4],
                    "confidence": 0.7 + (i % 3) * 0.1,
                },
            })
        elif remainder == 1:
            # intervention_plan
            iid = f"int_{i:04d}"
            intervention_ids.append(iid)
            events.append({
                "type": "intervention_plan",
                "timestamp": t,
                "payload": {
                    "intervention_id": iid,
                    "level": "overlay_only",
                },
            })
        elif remainder == 2:
            # intervention_outcome (alternate engaged / dismissed)
            action = "engaged" if i % 2 == 0 else "dismissed"
            events.append({
                "type": "intervention_outcome",
                "timestamp": t,
                "payload": {"user_action": action},
            })
        elif remainder == 3:
            # helpfulness
            reward = 0.5 if i % 2 == 0 else -0.3
            events.append({
                "type": "helpfulness",
                "timestamp": t,
                "payload": {"reward_signal": reward},
            })
        else:
            # another state event
            events.append({
                "type": "state",
                "timestamp": t,
                "payload": {
                    "state": "FLOW",
                    "confidence": 0.95,
                },
            })

    return events


def _write_jsonl(events: list[dict], path: Path) -> None:
    with path.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


@pytest.fixture
def harness():
    return ReplayHarness()


@pytest.fixture
def session_file(tmp_path):
    events = _generate_events(50)
    fpath = tmp_path / "session_test.jsonl"
    _write_jsonl(events, fpath)
    return fpath


# ---------------------------------------------------------------------------
# Load and verify event count
# ---------------------------------------------------------------------------

class TestLoad:
    def test_load_50_events(self, harness, session_file):
        events = harness.load(session_file)
        assert len(events) == 50

    def test_events_sorted_by_timestamp(self, harness, session_file):
        events = harness.load(session_file)
        timestamps = [e.timestamp for e in events]
        assert timestamps == sorted(timestamps)

    def test_event_types_present(self, harness, session_file):
        events = harness.load(session_file)
        types = {e.type for e in events}
        assert "state" in types
        assert "intervention_plan" in types
        assert "intervention_outcome" in types
        assert "helpfulness" in types

    def test_skip_blank_lines(self, harness, tmp_path):
        fpath = tmp_path / "blank.jsonl"
        with fpath.open("w") as f:
            f.write('{"type":"state","timestamp":1.0,"payload":{"state":"FLOW","confidence":0.9}}\n')
            f.write("\n")
            f.write('{"type":"state","timestamp":2.0,"payload":{"state":"HYPER","confidence":0.8}}\n')
        events = harness.load(fpath)
        assert len(events) == 2


# ---------------------------------------------------------------------------
# Replay and verify metrics
# ---------------------------------------------------------------------------

class TestReplay:
    def test_total_events(self, harness, session_file):
        events = harness.load(session_file)
        results = harness.replay(events, session_path=str(session_file))
        assert results.total_events == 50

    def test_state_event_count(self, harness, session_file):
        events = harness.load(session_file)
        results = harness.replay(events)
        # remainder 0 and 4 are state events -> 10 each = 20
        assert results.state_events == 20

    def test_intervention_count(self, harness, session_file):
        events = harness.load(session_file)
        results = harness.replay(events)
        # remainder 1 -> 10 intervention_plan events with unique ids
        assert results.intervention_count == 10
        assert results.intervention_events == 10

    def test_outcome_counts(self, harness, session_file):
        events = harness.load(session_file)
        results = harness.replay(events)
        # remainder 2 -> 10 outcome events
        assert results.outcome_events == 10
        assert results.engaged_count + results.dismissed_count == 10

    def test_helpfulness_metrics(self, harness, session_file):
        events = harness.load(session_file)
        results = harness.replay(events)
        # remainder 3 -> 10 helpfulness events
        assert results.helpfulness_events == 10
        assert len(results.rewards) == 10
        # Mean reward should be computable (not NaN)
        assert results.mean_reward == pytest.approx(
            sum(results.rewards) / len(results.rewards)
        )

    def test_mean_confidence(self, harness, session_file):
        events = harness.load(session_file)
        results = harness.replay(events)
        assert 0.0 < results.mean_confidence <= 1.0

    def test_state_distribution(self, harness, session_file):
        events = harness.load(session_file)
        results = harness.replay(events)
        assert isinstance(results.state_distribution, dict)
        assert sum(results.state_distribution.values()) == results.state_events


# ---------------------------------------------------------------------------
# Compare two runs
# ---------------------------------------------------------------------------

class TestCompare:
    def test_compare_reports_deltas(self, harness, tmp_path):
        # Baseline session
        baseline_events = _generate_events(50)
        baseline_path = tmp_path / "baseline.jsonl"
        _write_jsonl(baseline_events, baseline_path)

        # Variant session — modify helpfulness rewards to be higher
        variant_events = _generate_events(50)
        for ev in variant_events:
            if ev["type"] == "helpfulness":
                ev["payload"]["reward_signal"] = 0.9
        variant_path = tmp_path / "variant.jsonl"
        _write_jsonl(variant_events, variant_path)

        b_events = harness.load(baseline_path)
        v_events = harness.load(variant_path)

        b_results = harness.replay(b_events, session_path=str(baseline_path))
        v_results = harness.replay(v_events, session_path=str(variant_path))

        report = harness.compare(b_results, v_results)
        assert report.baseline is b_results
        assert report.variant is v_results
        # Variant has higher reward
        assert report.reward_delta > 0.0

    def test_compare_equal_sessions(self, harness, session_file):
        events = harness.load(session_file)
        r1 = harness.replay(events, session_path="run1")
        r2 = harness.replay(events, session_path="run2")

        report = harness.compare(r1, r2)
        assert report.reward_delta == pytest.approx(0.0)
        assert report.intervention_count_delta == 0
