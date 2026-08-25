"""Validate and evaluate a checksum-bound, participant-disjoint replay set."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from cortex.services.physio_engine.v2.replay import (
    DatasetManifestError,
    evaluate_dataset_manifest,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--split", choices=("development", "evaluation"), default="evaluation")
    parser.add_argument("--backend", default="pos")
    parser.add_argument("--window-seconds", type=float, default=10.0)
    parser.add_argument("--stride-seconds", type=float, default=1.0)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--minimum-coverage", type=float)
    parser.add_argument("--maximum-mae-bpm", type=float)
    parser.add_argument("--maximum-absolute-bias-bpm", type=float)
    parser.add_argument("--maximum-p95-error-bpm", type=float)
    return parser.parse_args()


def _gate_failures(report: dict[str, object], args: argparse.Namespace) -> list[str]:
    failures: list[str] = []
    gates = (
        ("coverage", args.minimum_coverage, lambda actual, limit: actual < limit),
        ("mae_bpm", args.maximum_mae_bpm, lambda actual, limit: actual > limit),
        (
            "bias_bpm",
            args.maximum_absolute_bias_bpm,
            lambda actual, limit: abs(actual) > limit,
        ),
        (
            "p95_absolute_error_bpm",
            args.maximum_p95_error_bpm,
            lambda actual, limit: actual > limit,
        ),
    )
    for field, limit, violates in gates:
        if limit is None:
            continue
        actual = report.get(field)
        if not isinstance(actual, (int, float)) or violates(float(actual), limit):
            failures.append(f"{field}={actual!r} violates configured limit {limit}")
    return failures


def main() -> int:
    args = _parse_args()
    try:
        report = evaluate_dataset_manifest(
            args.manifest,
            split=args.split,
            backend_name=args.backend,
            window_seconds=args.window_seconds,
            stride_seconds=args.stride_seconds,
        )
    except (DatasetManifestError, OSError, ValueError) as exc:
        print(f"dataset validation FAILED: {exc}", file=sys.stderr)
        return 2
    payload = asdict(report)
    payload["validation_gates"] = {
        "minimum_coverage": args.minimum_coverage,
        "maximum_mae_bpm": args.maximum_mae_bpm,
        "maximum_absolute_bias_bpm": args.maximum_absolute_bias_bpm,
        "maximum_p95_error_bpm": args.maximum_p95_error_bpm,
    }
    failures = _gate_failures(payload, args)
    payload["gate_failures"] = failures
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(rendered, encoding="utf-8")
        temporary.replace(args.output)
    print(rendered, end="")
    if failures:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
