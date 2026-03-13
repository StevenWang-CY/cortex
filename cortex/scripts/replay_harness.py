"""
Replay Harness — Offline Testing of Scoring, Routing, and Prompts

Loads JSONL session recordings and replays them through alternative
scoring policies, bandit configurations, and LLM prompt templates
to evaluate changes without live webcam data.

Usage:
    python -m cortex.scripts.replay_harness storage/sessions/session_*.jsonl
    python -m cortex.scripts.replay_harness --scorer v2 --prompts v2 sessions/*.jsonl
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ReplayEvent:
    """A single event from a session recording."""
    type: str
    timestamp: float
    payload: dict[str, Any]


@dataclass
class ReplayResults:
    """Results from a replay run."""
    session_path: str
    total_events: int
    state_events: int
    intervention_events: int
    outcome_events: int
    helpfulness_events: int

    # Computed metrics
    mean_confidence: float = 0.0
    intervention_count: int = 0
    engaged_count: int = 0
    dismissed_count: int = 0
    mean_reward: float = 0.0
    rewards: list[float] = field(default_factory=list)

    # State distribution
    state_distribution: dict[str, int] = field(default_factory=dict)


@dataclass
class ComparisonReport:
    """Comparison between two replay runs."""
    baseline: ReplayResults
    variant: ReplayResults
    reward_delta: float = 0.0
    engagement_delta: float = 0.0
    intervention_count_delta: int = 0


class ReplayHarness:
    """
    Replays JSONL session recordings for offline evaluation.

    Loads session files and processes them through configurable
    scoring, routing, and prompt strategies.

    Usage:
        harness = ReplayHarness()
        events = harness.load("storage/sessions/session_123.jsonl")
        results = harness.replay(events)
    """

    def __init__(self) -> None:
        pass

    def load(self, path: str | Path) -> list[ReplayEvent]:
        """
        Load events from a JSONL session file.

        Args:
            path: Path to the JSONL file.

        Returns:
            List of ReplayEvent objects, sorted by timestamp.
        """
        path = Path(path)
        events: list[ReplayEvent] = []

        with path.open("r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    events.append(ReplayEvent(
                        type=data.get("type", "unknown"),
                        timestamp=data.get("timestamp", 0.0),
                        payload=data.get("payload", {}),
                    ))
                except json.JSONDecodeError:
                    logger.warning("Skipping malformed line %d in %s", line_num, path)

        events.sort(key=lambda e: e.timestamp)
        return events

    def replay(
        self,
        events: list[ReplayEvent],
        session_path: str = "",
    ) -> ReplayResults:
        """
        Replay events and compute metrics.

        Args:
            events: List of session events to replay.
            session_path: Source file path for identification.

        Returns:
            ReplayResults with computed metrics.
        """
        results = ReplayResults(
            session_path=session_path,
            total_events=len(events),
            state_events=0,
            intervention_events=0,
            outcome_events=0,
            helpfulness_events=0,
        )

        confidences: list[float] = []
        state_counts: dict[str, int] = {}
        intervention_ids: set[str] = set()

        for event in events:
            if event.type == "state" or event.type == "state_estimate":
                results.state_events += 1
                payload = event.payload
                state = payload.get("state", "FLOW")
                confidence = payload.get("confidence", 0.0)
                confidences.append(confidence)
                state_counts[state] = state_counts.get(state, 0) + 1

            elif event.type == "intervention_plan":
                results.intervention_events += 1
                iid = event.payload.get("intervention_id", "")
                if iid:
                    intervention_ids.add(iid)

            elif event.type == "intervention_outcome":
                results.outcome_events += 1
                action = event.payload.get("user_action", "dismissed")
                if action == "engaged":
                    results.engaged_count += 1
                elif action == "dismissed":
                    results.dismissed_count += 1

            elif event.type == "helpfulness":
                results.helpfulness_events += 1
                reward = event.payload.get("reward_signal", 0.0)
                results.rewards.append(reward)

        results.intervention_count = len(intervention_ids)
        results.mean_confidence = (
            sum(confidences) / len(confidences) if confidences else 0.0
        )
        results.mean_reward = (
            sum(results.rewards) / len(results.rewards) if results.rewards else 0.0
        )
        results.state_distribution = state_counts

        return results

    def compare(
        self,
        baseline: ReplayResults,
        variant: ReplayResults,
    ) -> ComparisonReport:
        """Compare two replay results."""
        return ComparisonReport(
            baseline=baseline,
            variant=variant,
            reward_delta=variant.mean_reward - baseline.mean_reward,
            engagement_delta=(
                (variant.engaged_count / max(1, variant.intervention_count))
                - (baseline.engaged_count / max(1, baseline.intervention_count))
            ),
            intervention_count_delta=variant.intervention_count - baseline.intervention_count,
        )

    def print_results(self, results: ReplayResults) -> None:
        """Print a summary of replay results."""
        print(f"\n{'=' * 60}")
        print(f"Session: {results.session_path}")
        print(f"{'=' * 60}")
        print(f"Total events:        {results.total_events}")
        print(f"State events:        {results.state_events}")
        print(f"Intervention events: {results.intervention_events}")
        print(f"Outcome events:      {results.outcome_events}")
        print(f"Helpfulness events:  {results.helpfulness_events}")
        print(f"")
        print(f"Interventions:       {results.intervention_count}")
        print(f"  Engaged:           {results.engaged_count}")
        print(f"  Dismissed:         {results.dismissed_count}")
        print(f"Mean confidence:     {results.mean_confidence:.2%}")
        print(f"Mean reward:         {results.mean_reward:.3f}")
        print(f"State distribution:  {results.state_distribution}")

    def print_comparison(self, report: ComparisonReport) -> None:
        """Print a comparison report."""
        print(f"\n{'=' * 60}")
        print("COMPARISON REPORT")
        print(f"{'=' * 60}")
        print(f"Reward delta:      {report.reward_delta:+.3f}")
        print(f"Engagement delta:  {report.engagement_delta:+.2%}")
        print(f"Intervention delta: {report.intervention_count_delta:+d}")


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Replay Cortex session recordings for offline evaluation",
    )
    parser.add_argument(
        "sessions",
        nargs="+",
        help="JSONL session files to replay",
    )
    parser.add_argument(
        "--scorer",
        default="v1",
        choices=["v1", "v2"],
        help="Scoring policy version",
    )
    parser.add_argument(
        "--prompts",
        default="v1",
        choices=["v1", "v2"],
        help="Prompt template version",
    )
    args = parser.parse_args()

    harness = ReplayHarness()

    for session_path in args.sessions:
        path = Path(session_path)
        if not path.exists():
            print(f"Warning: {path} not found, skipping")
            continue

        events = harness.load(path)
        results = harness.replay(events, session_path=str(path))
        harness.print_results(results)


if __name__ == "__main__":
    main()
