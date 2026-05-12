"""
Eval — Policy replay utility for AMIP WAL logs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cortex.services.eval.amip import ARMS


def replay_policy_log(storage_root: str, day: str) -> dict[str, Any]:
    """
    Replay one daily policy log and return deterministic summary stats.
    """
    path = Path(storage_root) / "policy_log" / f"{day}.jsonl"
    if not path.exists():
        return {"day": day, "decisions": 0, "rewards": 0, "by_arm": dict.fromkeys(ARMS, 0)}

    by_arm = dict.fromkeys(ARMS, 0)
    reward_total = 0.0
    reward_count = 0
    decisions = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "action" in rec and "probabilities" in rec:
            decisions += 1
            action = str(rec.get("action"))
            if action in by_arm:
                by_arm[action] += 1
        if "reward" in rec:
            reward_total += float(rec.get("reward", 0.0))
            reward_count += 1

    return {
        "day": day,
        "decisions": decisions,
        "rewards": reward_count,
        "mean_reward": (reward_total / reward_count) if reward_count > 0 else 0.0,
        "by_arm": by_arm,
    }
