"""
Eval — Nightly causal report generator for AMIP logs.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from cortex.services.eval.amip import ARMS


def _load_policy_log(path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, float]]:
    decisions: dict[str, dict[str, Any]] = {}
    rewards: dict[str, float] = {}
    if not path.exists():
        return decisions, rewards
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        did = rec.get("decision_id")
        if not isinstance(did, str):
            continue
        if "probabilities" in rec and "action" in rec:
            decisions[did] = rec
        if "reward" in rec:
            rewards[did] = float(rec.get("reward", 0.0))
    return decisions, rewards


def _ips_snips(decisions: dict[str, dict[str, Any]], rewards: dict[str, float]) -> dict[str, dict[str, float]]:
    per_arm_num = defaultdict(float)
    per_arm_den = defaultdict(float)
    for did, dec in decisions.items():
        if did not in rewards:
            continue
        action = str(dec.get("action"))
        p = float(dec.get("probabilities", {}).get(action, 0.0))
        if p <= 1e-9:
            continue
        r = float(rewards[did])
        w = 1.0 / p
        per_arm_num[action] += r * w
        per_arm_den[action] += w

    out: dict[str, dict[str, float]] = {}
    for arm in ARMS:
        num = per_arm_num[arm]
        den = per_arm_den[arm]
        ips = num / max(1.0, len(decisions))
        snips = num / den if den > 1e-9 else 0.0
        out[arm] = {"ips": float(ips), "snips": float(snips), "weight": float(den)}
    return out


def _excursion_effect(decisions: dict[str, dict[str, Any]], rewards: dict[str, float]) -> dict[str, dict[str, float]]:
    by_arm = defaultdict(list)
    control = []
    for did, dec in decisions.items():
        if did not in rewards:
            continue
        arm = str(dec.get("action"))
        reward = float(rewards[did])
        if arm == "no_action":
            control.append(reward)
        else:
            by_arm[arm].append(reward)
    control_mean = float(np.mean(control)) if control else 0.0
    result: dict[str, dict[str, float]] = {}
    for arm in ARMS:
        if arm == "no_action":
            continue
        vals = np.array(by_arm.get(arm, []), dtype=np.float64)
        if vals.size == 0:
            result[arm] = {"effect": 0.0, "ci_low": 0.0, "ci_high": 0.0}
            continue
        effect = float(np.mean(vals) - control_mean)
        # Bootstrap CI
        boots = []
        for _ in range(200):
            sample = vals[np.random.randint(0, vals.size, vals.size)]
            boots.append(float(np.mean(sample) - control_mean))
        ci_low, ci_high = np.percentile(boots, [2.5, 97.5])
        result[arm] = {"effect": effect, "ci_low": float(ci_low), "ci_high": float(ci_high)}
    return result


def generate_daily_causal_report(storage_root: str, day: str | None = None) -> Path:
    """
    Generate `storage/reports/causal_<date>.md` from policy WAL logs.
    """
    day = day or datetime.now().strftime("%Y-%m-%d")
    root = Path(storage_root)
    log_path = root / "policy_log" / f"{day}.jsonl"
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    out_path = reports / f"causal_{day}.md"

    decisions, rewards = _load_policy_log(log_path)
    ips = _ips_snips(decisions, rewards)
    excursion = _excursion_effect(decisions, rewards)

    lines = [
        f"# Causal Report ({day})",
        "",
        f"- Decisions: {len(decisions)}",
        f"- Rewards linked: {len(rewards)}",
        "",
        "## IPS/SNIPS",
        "",
        "| Arm | IPS | SNIPS | Weight |",
        "|---|---:|---:|---:|",
    ]
    for arm in ARMS:
        row = ips.get(arm, {"ips": 0.0, "snips": 0.0, "weight": 0.0})
        lines.append(f"| {arm} | {row['ips']:.4f} | {row['snips']:.4f} | {row['weight']:.2f} |")

    lines.extend(["", "## Excursion Effect vs no_action", "", "| Arm | Effect | 95% CI |", "|---|---:|---:|"])
    for arm in ARMS:
        if arm == "no_action":
            continue
        row = excursion.get(arm, {"effect": 0.0, "ci_low": 0.0, "ci_high": 0.0})
        lines.append(f"| {arm} | {row['effect']:.4f} | [{row['ci_low']:.4f}, {row['ci_high']:.4f}] |")

    # Reliability proxy (Brier-like) when scores exist in WAL.
    brier_terms = []
    for did, dec in decisions.items():
        if did not in rewards:
            continue
        action = str(dec.get("action"))
        p = float(dec.get("probabilities", {}).get(action, 0.0))
        y = 1.0 if rewards[did] > 0 else 0.0
        brier_terms.append((p - y) ** 2)
    brier = float(np.mean(brier_terms)) if brier_terms else math.nan
    lines.extend(["", "## Calibration", "", f"- Brier-like score: {brier:.4f}" if not math.isnan(brier) else "- Brier-like score: n/a"])

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path
