"""
Cortex Session Replay — Load JSONL Session, Replay State Transitions

Loads a recorded session file (JSONL format) and replays state
transitions with timing visualization. Useful for debugging and
reviewing past sessions.

Session JSONL format (one JSON object per line):
    {"ts": 1234.5, "type": "state", "data": {...StateEstimate...}}
    {"ts": 1235.0, "type": "features", "data": {...FeatureVector...}}
    {"ts": 1236.0, "type": "transition", "data": {...StateTransition...}}
    {"ts": 1237.0, "type": "intervention", "data": {...InterventionOutcome...}}

Usage:
    python -m cortex.scripts.replay_session session.jsonl
    python -m cortex.scripts.replay_session session.jsonl --speed 2.0
    python -m cortex.scripts.replay_session session.jsonl --summary
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# State display colors (ANSI)
_STATE_COLORS = {
    "FLOW": "\033[92m",      # green
    "HYPO": "\033[94m",      # blue
    "HYPER": "\033[91m",     # red
    "RECOVERY": "\033[93m",  # yellow
}
_RESET = "\033[0m"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Cortex session replay",
    )
    parser.add_argument(
        "session_file", type=str,
        help="Path to session JSONL file",
    )
    parser.add_argument(
        "--speed", "-s", type=float, default=1.0,
        help="Playback speed multiplier (default: 1.0)",
    )
    parser.add_argument(
        "--summary", action="store_true",
        help="Show session summary without replay",
    )
    parser.add_argument(
        "--filter", "-f", type=str, default=None,
        choices=["state", "features", "transition", "intervention"],
        help="Only show events of this type",
    )
    parser.add_argument(
        "--no-color", action="store_true",
        help="Disable colored output",
    )
    parser.add_argument(
        "--instant", action="store_true",
        help="Replay without timing delays",
    )
    return parser.parse_args()


def load_session(path: str) -> list[dict]:
    """Load a JSONL session file."""
    file_path = Path(path)
    if not file_path.exists():
        print(f"ERROR: Session file not found: {path}")
        sys.exit(1)

    events: list[dict] = []
    line_num = 0

    with open(file_path) as f:
        for line in f:
            line_num += 1
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                event = json.loads(line)
                events.append(event)
            except json.JSONDecodeError as e:
                logger.warning("Skipping malformed line %d: %s", line_num, e)

    return events


def _format_state(
    state: str, confidence: float, *, color: bool = True
) -> str:
    if color and state in _STATE_COLORS:
        return f"{_STATE_COLORS[state]}{state}{_RESET} ({confidence:.2f})"
    return f"{state} ({confidence:.2f})"


def _format_timestamp(ts: float, start_ts: float) -> str:
    """Format timestamp as relative time from session start."""
    rel = ts - start_ts
    minutes = int(rel // 60)
    seconds = rel % 60
    return f"{minutes:02d}:{seconds:05.2f}"


def show_summary(events: list[dict]) -> None:
    """Display session summary statistics."""
    if not events:
        print("No events in session")
        return

    timestamps = [e.get("ts", 0) for e in events]
    start_ts = min(timestamps)
    end_ts = max(timestamps)
    duration = end_ts - start_ts

    # Count event types
    type_counts: dict[str, int] = {}
    for e in events:
        etype = e.get("type", "unknown")
        type_counts[etype] = type_counts.get(etype, 0) + 1

    # State distribution
    state_time: dict[str, float] = {}
    state_events = [e for e in events if e.get("type") == "state"]
    for i, e in enumerate(state_events):
        state = e.get("data", {}).get("state", "unknown")
        ts = e.get("ts", 0)
        next_ts = (
            state_events[i + 1].get("ts", ts)
            if i + 1 < len(state_events)
            else end_ts
        )
        dt = next_ts - ts
        state_time[state] = state_time.get(state, 0) + dt

    # Transition count
    transitions = [e for e in events if e.get("type") == "transition"]

    # Interventions
    interventions = [e for e in events if e.get("type") == "intervention"]

    print("=== Session Summary ===")
    print(f"  Duration:       {duration:.1f}s ({duration / 60:.1f} min)")
    print(f"  Total Events:   {len(events)}")
    print()
    print("  Event Types:")
    for etype, count in sorted(type_counts.items()):
        print(f"    {etype:15s}: {count}")
    print()

    if state_time:
        print("  State Distribution:")
        for state, t in sorted(state_time.items()):
            pct = (t / duration * 100) if duration > 0 else 0
            bar_len = int(pct / 2)
            bar = "#" * bar_len
            print(f"    {state:10s}: {t:6.1f}s ({pct:5.1f}%) {bar}")

    if transitions:
        print(f"\n  State Transitions: {len(transitions)}")
        for t in transitions:
            data = t.get("data", {})
            ts = _format_timestamp(t.get("ts", 0), start_ts)
            from_s = data.get("from_state", "?")
            to_s = data.get("to_state", "?")
            print(f"    [{ts}] {from_s} -> {to_s}")

    if interventions:
        print(f"\n  Interventions: {len(interventions)}")
        for iv in interventions:
            data = iv.get("data", {})
            ts = _format_timestamp(iv.get("ts", 0), start_ts)
            action = data.get("user_action", "?")
            dur = data.get("duration_seconds", 0)
            print(f"    [{ts}] action={action}, duration={dur:.1f}s")


def replay_session(
    events: list[dict],
    *,
    speed: float = 1.0,
    event_filter: str | None = None,
    color: bool = True,
    instant: bool = False,
) -> None:
    """Replay session events with timing."""
    if not events:
        print("No events to replay")
        return

    # Sort by timestamp
    events = sorted(events, key=lambda e: e.get("ts", 0))

    # Filter events
    if event_filter:
        events = [e for e in events if e.get("type") == event_filter]
        if not events:
            print(f"No '{event_filter}' events in session")
            return

    start_ts = events[0].get("ts", 0)
    prev_ts = start_ts
    total = len(events)

    print(f"Replaying {total} events (speed: {speed}x)")
    print("-" * 60)

    for i, event in enumerate(events):
        ts = event.get("ts", 0)
        etype = event.get("type", "unknown")
        data = event.get("data", {})

        # Timing delay
        if not instant and i > 0:
            delay = (ts - prev_ts) / speed
            if delay > 0:
                time.sleep(min(delay, 2.0))  # Cap max delay at 2s
        prev_ts = ts

        # Format timestamp
        ts_str = _format_timestamp(ts, start_ts)

        # Display based on type
        if etype == "state":
            state = data.get("state", "?")
            conf = data.get("confidence", 0)
            state_str = _format_state(state, conf, color=color)
            dwell = data.get("dwell_seconds", 0)
            print(f"[{ts_str}] STATE  {state_str}  dwell={dwell:.1f}s")

        elif etype == "transition":
            from_s = data.get("from_state", "?")
            to_s = data.get("to_state", "?")
            reasons = data.get("trigger_reasons", [])
            reason_str = ", ".join(reasons) if reasons else ""
            if color:
                to_colored = f"{_STATE_COLORS.get(to_s, '')}{to_s}{_RESET}"
                print(f"[{ts_str}] TRANS  {from_s} -> {to_colored}  {reason_str}")
            else:
                print(f"[{ts_str}] TRANS  {from_s} -> {to_s}  {reason_str}")

        elif etype == "features":
            hr = data.get("hr")
            blink = data.get("blink_rate")
            mouse = data.get("mouse_velocity_mean", 0)
            hr_str = f"HR={hr:.0f}" if hr is not None else "HR=--"
            blink_str = f"BR={blink:.1f}" if blink is not None else "BR=--"
            print(f"[{ts_str}] FEAT   {hr_str}  {blink_str}  Mouse={mouse:.0f}")

        elif etype == "intervention":
            action = data.get("user_action", "?")
            dur = data.get("duration_seconds", 0)
            recovered = data.get("recovery_detected", False)
            rec_str = " [RECOVERED]" if recovered else ""
            print(f"[{ts_str}] INTV   action={action} duration={dur:.1f}s{rec_str}")

        else:
            print(f"[{ts_str}] {etype.upper():6s} {json.dumps(data)[:80]}")

    print("-" * 60)
    print(f"Replay complete: {total} events")


def main() -> None:
    """Entry point for replay_session."""
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    args = _parse_args()

    events = load_session(args.session_file)
    print(f"Loaded {len(events)} events from {args.session_file}")

    color = not args.no_color

    if args.summary:
        show_summary(events)
    else:
        replay_session(
            events,
            speed=args.speed,
            event_filter=args.filter,
            color=color,
            instant=args.instant,
        )


if __name__ == "__main__":
    main()
