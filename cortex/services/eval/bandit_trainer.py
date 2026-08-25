"""Retired offline trainer for the pre-v2 contextual-bandit experiment.

The old observational helpfulness files do not contain the assignment,
availability, propensity, delivery, contamination, and reward-window data
required for defensible training or evaluation. Every training/evaluation
entry point therefore fails closed. Loading remains available only so legacy
records can be inventoried during migration.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any, NoReturn

logger = logging.getLogger(__name__)


class RetiredPolicyTrainingError(RuntimeError):
    """Raised when invalid legacy policy training is requested."""


_RETIRED_MESSAGE = (
    "retired: legacy helpfulness logs lack assignment, availability, propensity, "
    "delivery, contamination, and outcome-window records; use an immutable, "
    "separately consented MRT export and the prespecified research-analysis pipeline"
)


def load_training_data(data_dir: str) -> list[dict[str, Any]]:
    """Load helpfulness records from JSONL session files."""
    data_path = Path(data_dir)
    records: list[dict[str, Any]] = []

    for jsonl_file in sorted(data_path.glob("*.jsonl")):
        with jsonl_file.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    if event.get("type") == "helpfulness":
                        records.append(event.get("payload", {}))
                except json.JSONDecodeError:
                    continue

    logger.info("Loaded %d helpfulness records from %s", len(records), data_dir)
    return records


def train_bandit(records: list[dict[str, Any]], alpha: float = 1.0, epochs: int = 3) -> NoReturn:
    """Reject structurally invalid observational policy training."""

    del records, alpha, epochs
    raise RetiredPolicyTrainingError(_RETIRED_MESSAGE)


def evaluate_bandit(bandit: object, records: list[dict[str, Any]]) -> NoReturn:
    """Reject invalid evaluation of a policy trained from legacy logs."""

    del bandit, records
    raise RetiredPolicyTrainingError(_RETIRED_MESSAGE)


def main() -> None:
    """Refuse to train on structurally invalid legacy observational logs."""

    parser = argparse.ArgumentParser(description="Retired Cortex bandit trainer")
    parser.parse_args()
    parser.error(_RETIRED_MESSAGE)


if __name__ == "__main__":
    main()
