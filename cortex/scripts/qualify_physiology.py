"""Qualify camera physiology against known truth.

Two sources of truth, one production code path:

``synthetic``
    Simulated recordings with known heart rate, beat intervals and breathing
    rate, plus signal-free recordings that must not publish anything.  Runs
    anywhere, in seconds.

``dataset-4tu``
    The 4TU "Public Benchmark Dataset for Testing rPPG Algorithm
    Performance" (Hoffman & Lakens; doi:10.4121/uuid:2ac74fbd-2276-44ad-aff1-
    2f68972b7b51): 21 face videos of three participants with different skin
    tones under seven lighting conditions, head motion and a high heart rate,
    each with ECG RR intervals synchronised to the video.  The licence allows
    research use and forbids redistribution, so the data never enters this
    repository: download it yourself and pass ``--root``.

Every frame goes through the app's own face tracker, capture gate, skin
sampling and physiology processor (see ``services/eval``).  The output is a
JSON report and, with ``--markdown``, the tables the qualification document
quotes.

    python -m cortex.scripts.qualify_physiology synthetic --output out.json
    python -m cortex.scripts.qualify_physiology synthetic --check
    python -m cortex.scripts.qualify_physiology dataset-4tu \\
        --root "/path/to/Public Benchmark Dataset for Testing rPPG Algorithm Performance" \\
        --cache /path/to/trace-cache --output out.json --markdown
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Iterator
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from cortex._version import VERSION
from cortex.libs.config.settings import CortexConfig
from cortex.services.eval.physio_qualification import (
    METRIC_HEART_RATE,
    METRIC_RMSSD,
    METRIC_SDNN,
    MetricReport,
    assess,
    collect_pairs,
    false_publication_rate,
    merge_reports,
    replay_trace,
)
from cortex.services.eval.physio_synthetic import (
    SyntheticSubject,
    signal_free_recording,
    synthesize,
)

# Face width as a share of frame width after cropping: a laptop-webcam framing.
FACE_FRACTION = 0.29
CAPTURE_SIZE = (640, 480)
Pairs = dict[str, tuple[list[tuple[float, float]], int]]
METRIC_RESPIRATION = "respiration_rate_bpm"
# The metrics the support rules may read.  RMSSD is measured and reported, and
# is deliberately absent: it does not meet its criterion and nothing reads it.
ADMITTED_METRICS: tuple[str, ...] = (METRIC_HEART_RATE, METRIC_SDNN, METRIC_RESPIRATION)


def _report_rows(reports: dict[str, MetricReport]) -> dict[str, Any]:
    rows: dict[str, Any] = {}
    for name, report in reports.items():
        qualified, reasons = assess(report)
        rows[name] = {**report.to_dict(), "qualified": qualified, "reasons": reasons}
    return rows


# --- synthetic ---------------------------------------------------------------


def _constant(value: float) -> Callable[[float, float], float | None]:
    def _truth(_start: float, _end: float) -> float | None:
        return value

    return _truth


def run_synthetic() -> dict[str, Any]:
    collected: list[Pairs] = []
    seed = 0
    for heart_rate in (58.0, 72.0, 95.0):
        for rsa in (12.0, 35.0, 70.0):
            for noise in (0.0004, 0.0008):
                seed += 1
                subject = SyntheticSubject(
                    heart_rate_bpm=heart_rate,
                    rsa_amplitude_ms=rsa,
                    mayer_amplitude_ms=0.6 * rsa,
                    sensor_noise_fraction=noise,
                    respiration_rate_bpm=(10.0, 14.0, 19.0)[seed % 3],
                    duration_seconds=300.0,
                    seed=seed,
                )
                recording = synthesize(subject)
                records = replay_trace(
                    recording.sample_times_s,
                    recording.rgb,
                    recording.head_vertical_face_units,
                )
                collected.append(
                    collect_pairs(
                        records,
                        heart_rate_truth=recording.true_heart_rate_bpm,
                        rmssd_truth=recording.true_rmssd_ms,
                        sdnn_truth=recording.true_sdnn_ms,
                        respiration_truth=_constant(subject.respiration_rate_bpm),
                        warmup_seconds=12.0,
                    )
                )
    false_rates: dict[str, dict[str, float]] = {}
    for kind in ("white", "drift", "pink"):
        runs = []
        for free_seed in range(6):
            recording = signal_free_recording(
                kind=kind, seed=free_seed, duration_seconds=240.0
            )
            runs.append(
                false_publication_rate(
                    replay_trace(
                        recording.sample_times_s,
                        recording.rgb,
                        recording.head_vertical_face_units,
                    )
                )
            )
        false_rates[kind] = {
            metric: float(np.mean([run[metric] for run in runs])) for metric in runs[0]
        }
    return {
        "recordings": len(collected),
        "metrics": _report_rows(merge_reports(collected)),
        "false_publication_rate": false_rates,
    }


# --- 4TU dataset -------------------------------------------------------------


class _EcgTruth:
    """Truth from ECG RR intervals: ``times`` are beat instants in video time."""

    def __init__(self, path: Path) -> None:
        data = np.loadtxt(path)
        intervals_ms = data[:, 1] * 1000.0
        plausible = (intervals_ms > 300.0) & (intervals_ms < 2000.0)
        self._times = data[plausible, 0]
        self._intervals = intervals_ms[plausible]

    def _between(self, start: float, end: float) -> NDArray[np.float64]:
        inside = (self._times - self._intervals / 1000.0 >= start) & (self._times <= end)
        return np.asarray(self._intervals[inside], dtype=np.float64)

    def heart_rate(self, start: float, end: float) -> float | None:
        values = self._between(start, end)
        return float(60_000.0 / np.mean(values)) if len(values) >= 4 else None

    def rmssd(self, start: float, end: float) -> float | None:
        values = self._between(start, end)
        if len(values) < 30:
            return None
        return float(np.sqrt(np.mean(np.diff(values) ** 2)))

    def sdnn(self, start: float, end: float) -> float | None:
        values = self._between(start, end)
        return float(np.std(values, ddof=1)) if len(values) >= 40 else None


def _picture_band(frame: NDArray[np.uint8]) -> tuple[int, int]:
    """Rows of a letterboxed frame that hold the picture."""

    rows = np.flatnonzero(frame.mean(axis=(1, 2)) > 8.0)
    if len(rows) == 0:
        return 0, int(frame.shape[0])
    return int(rows[0]), int(rows[-1]) + 1


def _webcam_crop(video: Path) -> tuple[int, int, int, int]:
    """A 4:3 crop that frames the face as a laptop webcam would.

    The dataset's videos are letterboxed with the seated participant small in
    the picture.  The crop is fixed per video (taken from the median face box
    of a few frames found by the production tracker), so nothing here follows
    the face from frame to frame.
    """

    import cv2

    from cortex.services.capture_service.face_tracker import FaceTracker

    capture = cv2.VideoCapture(str(video))
    tracker = FaceTracker(CortexConfig().capture)
    tracker.initialize()
    boxes: list[tuple[float, float, float, float]] = []
    top = bottom = width = 0
    try:
        for index in range(0, 300, 30):
            capture.set(cv2.CAP_PROP_POS_FRAMES, index)
            ok, raw = capture.read()
            if not ok:
                break
            frame = np.asarray(raw, dtype=np.uint8)
            top, bottom = _picture_band(frame)
            width = int(frame.shape[1])
            result = tracker.process_frame(
                frame[top:bottom], capture_mono_ns=1_000_000_000 * (index + 1)
            )
            if result.landmarks_px is not None:
                points = np.asarray(result.landmarks_px)
                boxes.append(
                    (points[:, 0].min(), points[:, 0].max(),
                     points[:, 1].min(), points[:, 1].max())
                )
    finally:
        tracker.release()
        capture.release()
    if not boxes:
        raise RuntimeError(f"no face found in {video}")
    x0, x1, y0, y1 = (float(v) for v in np.median(np.asarray(boxes), axis=0))
    crop_w = (x1 - x0) / FACE_FRACTION
    crop_h = 0.75 * crop_w
    centre_x = 0.5 * (x0 + x1)
    centre_y = 0.5 * (y0 + y1) - 0.05 * crop_h
    left = int(np.clip(centre_x - crop_w / 2, 0, max(0, width - crop_w)))
    upper = int(np.clip(top + centre_y - crop_h / 2, top, max(top, bottom - crop_h)))
    return left, upper, int(crop_w), int(crop_h)


def _frames(video: Path) -> Iterator[tuple[float, NDArray[np.uint8]]]:
    import cv2

    left, upper, crop_w, crop_h = _webcam_crop(video)
    capture = cv2.VideoCapture(str(video))
    fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    index = 0
    try:
        while True:
            ok, raw = capture.read()
            if not ok:
                break
            cropped = raw[upper : upper + crop_h, left : left + crop_w]
            interpolation = (
                cv2.INTER_AREA if crop_w > CAPTURE_SIZE[0] else cv2.INTER_LINEAR
            )
            yield (
                index / fps,
                np.asarray(
                    cv2.resize(cropped, CAPTURE_SIZE, interpolation=interpolation),
                    dtype=np.uint8,
                ),
            )
            index += 1
    finally:
        capture.release()


def _extract_trace(job: tuple[str, str, str]) -> str:
    """Extract (or reuse) the capture-path trace of one video; returns its path."""

    from cortex.services.eval.physio_video_replay import extract_camera_trace

    name, video, cache = job
    target = Path(cache) / f"{name}.npz"
    if not target.exists():
        extract_camera_trace(_frames(Path(video))).save(str(target))
    return str(target)


def run_dataset_4tu(root: Path, cache: Path, workers: int) -> dict[str, Any]:
    from cortex.services.eval.physio_video_replay import (
        CameraTrace,
        replay_camera_trace,
    )

    cache.mkdir(parents=True, exist_ok=True)
    jobs: list[tuple[str, str, str]] = []
    truths: dict[str, _EcgTruth] = {}
    for folder in sorted(path for path in root.iterdir() if path.is_dir()):
        videos = sorted(folder.glob("*dited.avi"))
        references = sorted(folder.glob("*.rr"))
        if videos and references:
            jobs.append((folder.name, str(videos[0]), str(cache)))
            truths[folder.name] = _EcgTruth(references[0])
    if not jobs:
        raise SystemExit(f"no recordings with video and RR intervals under {root}")
    with ProcessPoolExecutor(max_workers=max(1, workers)) as executor:
        trace_paths = dict(zip((job[0] for job in jobs), executor.map(_extract_trace, jobs), strict=True))

    per_recording: dict[str, Pairs] = {}
    recordings: dict[str, Any] = {}
    for name, trace_path in trace_paths.items():
        trace = CameraTrace.load(trace_path)
        truth = truths[name]
        pairs = collect_pairs(
            replay_camera_trace(trace),
            heart_rate_truth=truth.heart_rate,
            rmssd_truth=truth.rmssd,
            sdnn_truth=truth.sdnn,
            warmup_seconds=12.0,
        )
        per_recording[name] = pairs
        recordings[name] = {
            "usable_frame_fraction": trace.valid_fraction,
            "metrics": {
                metric: report.to_dict()
                for metric, report in merge_reports([pairs]).items()
            },
        }

    def _group(names: list[str]) -> dict[str, Any]:
        return _report_rows(merge_reports([per_recording[name] for name in names]))

    everything = sorted(per_recording)
    participants = sorted({name[:2] for name in everything})
    return {
        "recordings": recordings,
        "pooled": _group(everything),
        "by_condition": {
            "lighting": _group([name for name in everything if "LC" in name]),
            "head_motion": _group([name for name in everything if "M" in name[2:]]),
            "high_heart_rate": _group([name for name in everything if "H" in name[2:]]),
        },
        "by_participant": {
            participant: _group(
                [name for name in everything if name.startswith(participant)]
            )
            for participant in participants
        },
    }


def synthetic_gate_failures(synthetic: dict[str, Any]) -> list[str]:
    """Why a simulated run does not support the admitted metrics; empty when it does.

    Every admitted metric must meet its acceptance criterion, and nothing at
    all may be published on a recording that contains no pulse.
    """
    failures: list[str] = []
    metrics = synthetic.get("metrics", {})
    for metric in ADMITTED_METRICS:
        row = metrics.get(metric)
        if row is None:
            failures.append(f"{metric}: no result")
        elif not row.get("qualified"):
            reasons = "; ".join(row.get("reasons") or ["criterion not met"])
            failures.append(f"{metric}: {reasons}")
    for kind, rates in sorted(synthetic.get("false_publication_rate", {}).items()):
        for metric, rate in sorted(rates.items()):
            if rate > 0.0:
                failures.append(
                    f"{metric}: published on {rate:.2%} of strides of signal-free "
                    f"'{kind}' recordings"
                )
    return failures


# --- output ------------------------------------------------------------------


def _markdown_table(title: str, rows: dict[str, Any]) -> str:
    lines = [
        f"**{title}**",
        "",
        "| Metric | Published / possible | Coverage | MAE | Bias | 95th pct error | r | Within tolerance | Meets criterion |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]

    def _number(value: float | None, digits: int = 2) -> str:
        return "—" if value is None else f"{value:.{digits}f}"

    for metric in (METRIC_HEART_RATE, METRIC_SDNN, METRIC_RMSSD, METRIC_RESPIRATION):
        row = rows.get(metric)
        if row is None:
            continue
        lines.append(
            f"| {metric} | {row['published']} / {row['attempted']} | "
            f"{_number(row['coverage'])} | {_number(row['mae'])} | "
            f"{_number(row['bias'])} | {_number(row['p95_abs_error'])} | "
            f"{_number(row['pearson_r'])} | {_number(row['within_tolerance'])} "
            f"(±{row['tolerance']:g}) | {'yes' if row.get('qualified') else 'no'} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    synthetic = sub.add_parser("synthetic", help="simulated recordings with known truth")
    dataset = sub.add_parser("dataset-4tu", help="4TU rPPG benchmark with ECG reference")
    dataset.add_argument("--root", type=Path, required=True)
    dataset.add_argument("--cache", type=Path, required=True)
    dataset.add_argument("--workers", type=int, default=6)
    synthetic.add_argument(
        "--check",
        action="store_true",
        help=(
            "exit 1 unless heart rate, SDNN and breathing rate meet their criteria "
            "and nothing is published on signal-free recordings"
        ),
    )
    for item in (synthetic, dataset):
        item.add_argument("--output", type=Path)
        item.add_argument("--markdown", action="store_true")
    args = parser.parse_args(argv)

    config = CortexConfig()
    result: dict[str, Any] = {
        "generated_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cortex_version": VERSION,
        "mode": args.mode,
        "rppg_config": config.signal.rppg.model_dump(mode="json"),
    }
    if args.mode == "synthetic":
        result["synthetic"] = run_synthetic()
    else:
        result["dataset_4tu"] = run_dataset_4tu(args.root, args.cache, args.workers)

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    if args.markdown:
        if args.mode == "synthetic":
            print(_markdown_table("Synthetic truth", result["synthetic"]["metrics"]))
            print()
            print(json.dumps(result["synthetic"]["false_publication_rate"], indent=2))
        else:
            data = result["dataset_4tu"]
            print(_markdown_table("All 21 recordings", data["pooled"]))
            for label, rows in data["by_condition"].items():
                print()
                print(_markdown_table(label.replace("_", " ").capitalize(), rows))
            for label, rows in data["by_participant"].items():
                print()
                print(_markdown_table(f"Participant {label}", rows))
    elif args.output is None and not getattr(args, "check", False):
        json.dump(result, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    if getattr(args, "check", False):
        failures = synthetic_gate_failures(result["synthetic"])
        if failures:
            for failure in failures:
                print(f"physiology qualification failed: {failure}", file=sys.stderr)
            return 1
        print(
            "physiology qualification (simulated): "
            + ", ".join(ADMITTED_METRICS)
            + " meet their criteria; nothing published on signal-free recordings"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
