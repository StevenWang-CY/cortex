"""
Cortex Calibration — measured multi-phase profile capture (CLI wrapper)

P0 §3.4: the live + simulate loops now live in
:mod:`cortex.services.capture_service.calibration_runner` so the desktop
shell wizard, the Settings "Recalibrate" button, and this script all
drive the same code path. This file is reduced to argument parsing,
runner instantiation, and result printing.

Usage:
    python -m cortex.scripts.calibrate
    python -m cortex.scripts.calibrate --simulate --duration 3
    cortex-calibrate  # if installed via pip
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from cortex.libs.schemas.state import UserBaselines
from cortex.services.capture_service.calibration_runner import (
    DEFAULT_DURATION_SECONDS,
    CalibrationProgress,
    CalibrationRunner,
    compute_baselines,
)

logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Cortex calibration — capture personal baselines",
    )
    parser.add_argument(
        "--duration", "-d", type=int, default=DEFAULT_DURATION_SECONDS,
        help=f"Calibration duration in seconds (default: {DEFAULT_DURATION_SECONDS})",
    )
    parser.add_argument(
        "--output", "-o", type=str, default=None,
        help="Optional copy of the immutable profile JSON (the canonical profile remains in storage)",
    )
    parser.add_argument(
        "--simulate", action="store_true",
        help="Preview a demo profile with synthetic data; never activates calibration",
    )
    parser.add_argument(
        "--approve",
        action="store_true",
        help="Explicitly approve a measured profile after capture (for non-interactive use)",
    )
    parser.add_argument(
        "--save-demo",
        action="store_true",
        help="Persist a simulated demo profile in the isolated demo namespace",
    )
    return parser.parse_args()


# Re-exports preserved for any pre-3.4 callers / tests that imported
# private helpers from this module directly.
__all__ = [
    "_simulate_calibration",
    "compute_baselines",
    "main",
    "save_baselines",
]


def _simulate_calibration(duration_seconds: int) -> dict[str, list[float]]:
    """Synchronous backwards-compat shim around ``run_simulate_calibration``.

    Pre-3.4 the simulate loop lived here and was importable directly.
    The runner refactor made it async; this shim drives the async loop
    via ``asyncio.run`` so the legacy test signature continues to work.
    """
    from cortex.services.capture_service.calibration_runner import (
        run_simulate_calibration,
    )

    return asyncio.run(run_simulate_calibration(duration_seconds))


def save_baselines(baselines: UserBaselines, output_path: str | None = None) -> Path:
    """Compatibility shim. Pre-3.4 the CLI exposed this helper publicly;
    a couple of in-tree tools call it. Routes through CalibrationRunner's
    atomic-write so we never get split write semantics."""
    runner = CalibrationRunner(
        duration_seconds=1,
        simulate=True,
        output_path=output_path,
    )
    # Bypass the start()/finish() lifecycle — we already have the result.
    return runner._write_baselines(baselines)  # noqa: SLF001 - thin wrapper


def _cli_progress(progress: CalibrationProgress) -> None:
    """Stream progress to stdout. The CLI doesn't need a fancy bar — a
    one-line update every ~5% is plenty for an operator watching the
    terminal."""
    pct = progress.pct_complete
    elapsed = progress.elapsed_seconds
    total = progress.total_seconds
    # Print at every 5% step (and at start/end) to avoid flooding the log.
    if int(pct) % 5 == 0 and progress.status == "running":
        hr = f"{progress.current_hr:.0f}" if progress.current_hr is not None else "—"
        sqi = f"{progress.current_sqi:.2f}" if progress.current_sqi is not None else "—"
        print(
            f"  [{pct:5.1f}%] {elapsed:5.1f}s / {total:.0f}s  "
            f"HR: {hr:>3} bpm  SQI: {sqi}"
        )


async def _run(args: argparse.Namespace) -> None:
    runner = CalibrationRunner(
        duration_seconds=args.duration,
        simulate=args.simulate,
        output_path=args.output,
    )

    print("=" * 50)
    print("  Cortex Calibration")
    print("=" * 50)
    print(f"  Duration: {args.duration}s")
    print(f"  Mode:     {'simulation' if args.simulate else 'live'}")
    print("=" * 50)

    await runner.start(on_progress=_cli_progress)
    profile = runner.preview_profile()

    print("\n--- Calibration Review ---")
    for metric in profile.metrics:
        value = "unavailable" if metric.value is None else f"{metric.value:.2f} {metric.unit}"
        print(
            f"  {str(metric.metric):30s} {value:20s} "
            f"[{metric.maturity}; {metric.reference_task}]"
        )
    print("  Raw frames, landmarks, key contents, and workspace text are not stored.")
    print("  Webcam heart/respiration estimates remain experimental and are not used for scoring.")

    if args.simulate:
        if args.save_demo:
            demo = await runner.save_demo()
            print(f"\nDemo profile saved (inactive): {demo.profile_id}")
        else:
            print("\nDemo preview only. No active calibration was written.")
        return

    approved = bool(args.approve)
    if not approved and sys.stdin.isatty():
        response = input("\nSave and apply this measured profile? [y/N] ").strip().lower()
        approved = response in {"y", "yes"}
    if not approved:
        print("\nNot saved: explicit approval was not provided.")
        return
    committed = await runner.finish(approved_by_user=True)
    runner.activate_for_next_start(str(committed.profile_id))
    print(f"\nMeasured profile activated for the next daemon start: {committed.profile_id}")
    print("If Cortex is already running, restart it to apply this CLI-created profile.")


def main() -> None:
    """Entry point for cortex-calibrate command."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    args = _parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
