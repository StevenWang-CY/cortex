"""Descriptive policy operations report with explicit evidence limits."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, date, datetime, tzinfo
from pathlib import Path

from cortex.libs.utils.atomic_write import atomic_write_text
from cortex.services.eval.policy_repository import PolicyRepository


def _day_for_unix_ms(value: int, tz: tzinfo = UTC) -> str:
    return datetime.fromtimestamp(value / 1_000, tz=UTC).astimezone(tz).date().isoformat()


def _validated_day(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("day must be a real ISO calendar date") from exc
    if parsed.isoformat() != value:
        raise ValueError("day must use canonical YYYY-MM-DD form")
    return value


async def generate_daily_policy_diagnostics(
    repository: PolicyRepository,
    storage_root: str | Path,
    *,
    day: str | None = None,
    tz: tzinfo | None = None,
) -> Path:
    """Write counts/completeness only—never an effect or causal estimate.

    ``day`` is interpreted in ``tz`` (default UTC). The daemon's nightly run
    passes the completed *local* day together with the local zone so the
    report window matches the local-hour schedule that triggers it
    (audit D12: the old call gated on the local hour but bucketed by UTC day).
    """

    zone: tzinfo = tz if tz is not None else UTC
    target_day = (
        _validated_day(day) if day is not None else datetime.now(tz=zone).date().isoformat()
    )
    rows = [
        row
        for row in await repository.export_rows()
        if _day_for_unix_ms(int(row["decision"]["occurred_at_unix_ms"]), zone) == target_day
    ]
    modes = Counter(str(row["decision"].get("policy_mode")) for row in rows)
    arms = Counter(str(row["decision"].get("selected_arm")) for row in rows)
    deliveries = Counter(
        str((row.get("delivery") or {}).get("status") or "missing") for row in rows
    )
    outcomes = Counter(
        str((row.get("outcome") or {}).get("status") or "pending_or_missing") for row in rows
    )
    reward_values = [float(row["reward"]["value"]) for row in rows if row.get("reward")]
    randomized = sum(
        1 for row in rows if row["decision"].get("policy_mode") == "research_randomized"
    )
    ope_eligible = sum(
        1
        for row in rows
        if row["decision"].get("supports_ope") is True
        and isinstance(row["decision"].get("propensities"), dict)
    )
    mean_reward = sum(reward_values) / len(reward_values) if reward_values else None
    lines = [
        f"# Policy diagnostics ({target_day})",
        "",
        "> Descriptive operations report only. It does not estimate a causal effect, "
        "policy value, treatment benefit, or calibrated probability.",
        "",
        "## Lifecycle completeness",
        "",
        f"- Decision points: {len(rows)}",
        f"- Delivery records: {sum(deliveries.values()) - deliveries['missing']}",
        f"- Finalized/censored outcomes: {sum(v for k, v in outcomes.items() if k in {'finalized', 'censored'})}",
        f"- Finalized rewards: {len(reward_values)}",
        f"- Randomized research records: {randomized}",
        f"- Records structurally eligible for OPE input: {ope_eligible}",
        (
            f"- Mean finalized reward (descriptive): {mean_reward:.4f}"
            if mean_reward is not None
            else "- Mean finalized reward (descriptive): n/a"
        ),
        "",
        "## Counts by policy mode",
        "",
        "| Mode | Count |",
        "|---|---:|",
    ]
    lines.extend(f"| {name} | {count} |" for name, count in sorted(modes.items()))
    lines.extend(["", "## Counts by selected arm", "", "| Arm | Count |", "|---|---:|"])
    lines.extend(f"| {name} | {count} |" for name, count in sorted(arms.items()))
    lines.extend(["", "## Delivery status", "", "| Status | Count |", "|---|---:|"])
    lines.extend(f"| {name} | {count} |" for name, count in sorted(deliveries.items()))
    lines.extend(["", "## Outcome status", "", "| Status | Count |", "|---|---:|"])
    lines.extend(f"| {name} | {count} |" for name, count in sorted(outcomes.items()))
    lines.extend(
        [
            "",
            "## Interpretation boundary",
            "",
            "Deterministic production records have no research propensity and must not be "
            "used for IPS, SNIPS, doubly robust, excursion-effect, or MRT claims. A "
            "research estimate requires a separately consented immutable export with its "
            "prespecified study contract and complete no-action follow-up.",
        ]
    )
    reports = Path(storage_root).expanduser().resolve() / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    destination = reports / f"policy_diagnostics_{target_day}.md"
    atomic_write_text(destination, "\n".join(lines) + "\n")
    return destination


def migrate_legacy_causal_report_names(storage_root: str | Path) -> list[Path]:
    """Rename old report artifacts without reclassifying them as evidence."""

    reports = Path(storage_root).expanduser().resolve() / "reports"
    if not reports.exists():
        return []
    migrated: list[Path] = []
    for source in sorted(reports.glob("causal_*.md")):
        suffix = source.name.removeprefix("causal_")
        destination = reports / f"legacy_policy_diagnostics_{suffix}"
        if destination.exists():
            continue
        source.replace(destination)
        migrated.append(destination)
    return migrated


def generate_legacy_policy_diagnostics(
    storage_root: str | Path,
    *,
    day: str,
) -> Path:
    """Summarize legacy AMIP JSONL without calculating invalid estimators."""

    day = _validated_day(day)
    root = Path(storage_root).expanduser().resolve()
    path = root / "policy_log" / f"{day}.jsonl"
    decisions = 0
    reward_events = 0
    unique_reward_decisions: set[str] = set()
    duplicate_reward_events = 0
    arms: Counter[str] = Counter()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            decision_id = str(record.get("decision_id") or "")
            if "probabilities" in record and "action" in record:
                decisions += 1
                arms[str(record.get("action"))] += 1
            if "reward" in record:
                reward_events += 1
                if decision_id in unique_reward_decisions:
                    duplicate_reward_events += 1
                unique_reward_decisions.add(decision_id)
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    destination = reports / f"legacy_policy_diagnostics_{day}.md"
    lines = [
        f"# Legacy policy diagnostics ({day})",
        "",
        "> Legacy, incomplete adaptive-policy log. Not valid causal or OPE evidence.",
        "",
        f"- Decision events: {decisions}",
        f"- Reward update events: {reward_events}",
        f"- Unique rewarded decisions: {len(unique_reward_decisions)}",
        f"- Duplicate reward updates: {duplicate_reward_events}",
        "- No-action outcome completeness: unknown",
        "- Eligibility/availability completeness: unknown",
        "",
        "| Arm | Decisions |",
        "|---|---:|",
    ]
    lines.extend(f"| {arm} | {count} |" for arm, count in sorted(arms.items()))
    atomic_write_text(destination, "\n".join(lines) + "\n")
    return destination


__all__ = [
    "generate_daily_policy_diagnostics",
    "generate_legacy_policy_diagnostics",
    "migrate_legacy_causal_report_names",
]
