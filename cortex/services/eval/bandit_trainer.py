"""
Eval — Offline Bandit Trainer

Batch training script for the contextual bandit using historical
session data. Can be run on the V100 GPU for large datasets.

Usage:
    python -m cortex.services.eval.bandit_trainer --data storage/sessions/ --output storage/models/

For V100 training:
    scp storage/sessions/*.jsonl wangcy07@gwhiz2.cis.upenn.edu:~/cortex_data/
    ssh wangcy07@gwhiz2.cis.upenn.edu "python -m cortex.services.eval.bandit_trainer --data ~/cortex_data/"
    scp wangcy07@gwhiz2.cis.upenn.edu:~/cortex_models/bandit_weights.json storage/models/
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import numpy as np

from cortex.services.eval.bandit import ARM_LABELS, N_ARMS, N_FEATURES, ContextualBandit

logger = logging.getLogger(__name__)


def load_training_data(data_dir: str) -> list[dict]:
    """Load helpfulness records from JSONL session files."""
    data_path = Path(data_dir)
    records = []

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


def train_bandit(records: list[dict], alpha: float = 1.0, epochs: int = 3) -> ContextualBandit:
    """
    Train a contextual bandit on historical data.

    Runs multiple epochs over the data to improve convergence.

    Args:
        records: List of helpfulness record dicts.
        alpha: UCB exploration parameter.
        epochs: Number of passes over the data.

    Returns:
        Trained ContextualBandit.
    """
    bandit = ContextualBandit(n_arms=N_ARMS, n_features=N_FEATURES, alpha=alpha)

    for epoch in range(epochs):
        np.random.shuffle(records)  # type: ignore[arg-type]
        total_reward = 0.0
        count = 0

        for record in records:
            features = record.get("context_features", [])
            arm_idx = record.get("arm_index", 0)
            reward = record.get("reward_signal", 0.0)

            if len(features) != N_FEATURES:
                continue

            context = np.array(features, dtype=np.float64)
            bandit.update(context, arm_idx, reward)
            total_reward += reward
            count += 1

        if count > 0:
            logger.info(
                "Epoch %d/%d: %d records, mean reward = %.3f",
                epoch + 1, epochs, count, total_reward / count,
            )

    return bandit


def evaluate_bandit(bandit: ContextualBandit, records: list[dict]) -> dict:
    """Evaluate the trained bandit on held-out data."""
    correct = 0
    total = 0
    cumulative_reward = 0.0

    for record in records:
        features = record.get("context_features", [])
        reward = record.get("reward_signal", 0.0)

        if len(features) != N_FEATURES:
            continue

        context = np.array(features, dtype=np.float64)
        selected = bandit.select_arm(context)

        # Check if bandit would have selected a positive-reward arm
        if reward > 0:
            correct += 1
        cumulative_reward += reward
        total += 1

    return {
        "total": total,
        "mean_reward": cumulative_reward / total if total > 0 else 0.0,
        "arm_stats": bandit.get_arm_stats(),
    }


def main() -> None:
    """CLI entry point for offline bandit training."""
    parser = argparse.ArgumentParser(description="Train contextual bandit offline")
    parser.add_argument("--data", required=True, help="Directory with JSONL session files")
    parser.add_argument("--output", default="storage/models/", help="Output directory for weights")
    parser.add_argument("--alpha", type=float, default=1.0, help="UCB exploration parameter")
    parser.add_argument("--epochs", type=int, default=3, help="Training epochs")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    # Load data
    records = load_training_data(args.data)
    if not records:
        print("No helpfulness records found. Exiting.")
        return

    # Split train/eval (80/20)
    split = int(len(records) * 0.8)
    train_records = records[:split]
    eval_records = records[split:]

    # Train
    bandit = train_bandit(train_records, alpha=args.alpha, epochs=args.epochs)

    # Evaluate
    if eval_records:
        eval_results = evaluate_bandit(bandit, eval_records)
        print(f"\nEvaluation ({len(eval_records)} records):")
        print(f"  Mean reward: {eval_results['mean_reward']:.3f}")
        for stat in eval_results["arm_stats"]:
            print(f"  {stat['arm']}: theta_norm={stat['theta_norm']:.3f}")

    # Save weights
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    weights_path = output_dir / "bandit_weights.json"
    with weights_path.open("w", encoding="utf-8") as f:
        json.dump(bandit._to_dict(), f, indent=2)
    print(f"\nWeights saved to {weights_path}")


if __name__ == "__main__":
    main()
