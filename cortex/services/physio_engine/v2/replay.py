"""Checksum-verified, subject-disjoint physiology dataset replay."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID

import numpy as np
from numpy.typing import NDArray

from cortex.services.physio_engine.v2.backends import RPPGBackendRegistry
from cortex.services.physio_engine.v2.pulse import PulsePipelineV2

DatasetSplit = Literal["development", "evaluation"]


class DatasetManifestError(ValueError):
    """A replay manifest or referenced trace violates the data contract."""


@dataclass(frozen=True)
class DatasetSequence:
    subject_id: str
    sequence_id: str
    split: DatasetSplit
    path: Path
    sha256: str
    sample_rate_hz: float


@dataclass(frozen=True)
class DatasetManifest:
    dataset_name: str
    dataset_version: str
    license_name: str
    source_url: str
    sequences: tuple[DatasetSequence, ...]


@dataclass(frozen=True)
class ReplayReport:
    dataset_name: str
    dataset_version: str
    split: DatasetSplit
    subject_count: int
    sequence_count: int
    attempted_windows: int
    accepted_windows: int
    coverage: float
    mae_bpm: float | None
    rmse_bpm: float | None
    correlation: float | None
    bias_bpm: float | None
    loa_lower_bpm: float | None
    loa_upper_bpm: float | None
    backend_name: str
    backend_version: str
    backend_sha256: str


def _require_string(raw: object, field: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise DatasetManifestError(f"{field} must be a non-empty string")
    return raw.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_dataset_manifest(path: Path) -> DatasetManifest:
    """Load, contain and checksum every trace before any metric is computed."""

    manifest_path = path.resolve()
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DatasetManifestError(f"cannot read dataset manifest: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != "1.0":
        raise DatasetManifestError("dataset manifest schema_version must be '1.0'")
    raw_sequences = raw.get("sequences")
    if not isinstance(raw_sequences, list) or not raw_sequences:
        raise DatasetManifestError("dataset manifest requires sequences")
    root = manifest_path.parent
    sequences: list[DatasetSequence] = []
    subject_splits: dict[str, set[str]] = {}
    sequence_ids: set[str] = set()
    for index, item in enumerate(raw_sequences):
        if not isinstance(item, dict):
            raise DatasetManifestError(f"sequences[{index}] must be an object")
        subject_id = _require_string(item.get("subject_id"), "subject_id")
        sequence_id = _require_string(item.get("sequence_id"), "sequence_id")
        if sequence_id in sequence_ids:
            raise DatasetManifestError(f"duplicate sequence_id: {sequence_id}")
        sequence_ids.add(sequence_id)
        split = item.get("split")
        if split not in {"development", "evaluation"}:
            raise DatasetManifestError(f"invalid split for {sequence_id}")
        relative_path = Path(_require_string(item.get("path"), "path"))
        trace_path = (root / relative_path).resolve()
        try:
            trace_path.relative_to(root)
        except ValueError as exc:
            raise DatasetManifestError(
                f"sequence path escapes manifest directory: {relative_path}"
            ) from exc
        expected_sha = _require_string(item.get("sha256"), "sha256")
        if len(expected_sha) != 64 or any(c not in "0123456789abcdef" for c in expected_sha):
            raise DatasetManifestError(f"invalid SHA-256 for {sequence_id}")
        if not trace_path.is_file():
            raise DatasetManifestError(f"trace is missing: {relative_path}")
        actual_sha = _sha256(trace_path)
        if actual_sha != expected_sha:
            raise DatasetManifestError(f"checksum mismatch for {sequence_id}")
        raw_sample_rate = item.get("sample_rate_hz")
        if isinstance(raw_sample_rate, bool) or not isinstance(
            raw_sample_rate, (int, float)
        ):
            raise DatasetManifestError(f"invalid sample_rate_hz for {sequence_id}")
        sample_rate_hz = float(raw_sample_rate)
        if not 1.0 <= sample_rate_hz <= 240.0:
            raise DatasetManifestError(f"implausible sample_rate_hz for {sequence_id}")
        subject_splits.setdefault(subject_id, set()).add(str(split))
        sequences.append(
            DatasetSequence(
                subject_id=subject_id,
                sequence_id=sequence_id,
                split=split,
                path=trace_path,
                sha256=expected_sha,
                sample_rate_hz=sample_rate_hz,
            )
        )
    leaking = sorted(subject for subject, splits in subject_splits.items() if len(splits) > 1)
    if leaking:
        raise DatasetManifestError(
            f"subjects occur in both development and evaluation splits: {leaking}"
        )
    return DatasetManifest(
        dataset_name=_require_string(raw.get("dataset_name"), "dataset_name"),
        dataset_version=_require_string(raw.get("dataset_version"), "dataset_version"),
        license_name=_require_string(raw.get("license_name"), "license_name"),
        source_url=_require_string(raw.get("source_url"), "source_url"),
        sequences=tuple(sequences),
    )


def _load_trace(sequence: DatasetSequence) -> tuple[NDArray[np.float64], float]:
    try:
        with np.load(sequence.path, allow_pickle=False) as archive:
            rgb = np.asarray(archive["rgb_trace"], dtype=np.float64)
            reference = np.asarray(archive["hr_gt"], dtype=np.float64).reshape(-1)
    except (OSError, KeyError, ValueError) as exc:
        raise DatasetManifestError(
            f"invalid trace {sequence.sequence_id}: {exc}"
        ) from exc
    if rgb.ndim != 2 or rgb.shape[1] != 3 or len(rgb) < 2:
        raise DatasetManifestError(
            f"{sequence.sequence_id} rgb_trace must have shape [samples, 3]"
        )
    finite_reference = reference[np.isfinite(reference)]
    if not bool(np.isfinite(rgb).all()) or len(finite_reference) == 0:
        raise DatasetManifestError(
            f"{sequence.sequence_id} contains no finite signal/reference"
        )
    return rgb, float(np.mean(finite_reference))


def evaluate_dataset_manifest(
    path: Path,
    *,
    split: DatasetSplit = "evaluation",
    backend_name: str = "pos",
    window_seconds: float = 10.0,
    stride_seconds: float = 1.0,
) -> ReplayReport:
    """Replay one held-out split and report accuracy plus abstention coverage."""

    if window_seconds <= 0 or stride_seconds <= 0:
        raise ValueError("replay window and stride must be positive")
    manifest = load_dataset_manifest(path)
    selected = [item for item in manifest.sequences if item.split == split]
    if not selected:
        raise DatasetManifestError(f"manifest has no {split} sequences")
    backend = RPPGBackendRegistry.with_packaged_backends().resolve(backend_name)
    predicted: list[float] = []
    reference: list[float] = []
    attempted = 0
    for sequence in selected:
        rgb, reference_bpm = _load_trace(sequence)
        fs = sequence.sample_rate_hz
        window_samples = max(2, int(round(window_seconds * fs)))
        stride_samples = max(1, int(round(stride_seconds * fs)))
        if len(rgb) < window_samples:
            continue
        pipeline = PulsePipelineV2(backend)
        boot_id = UUID(hex=hashlib.sha256(sequence.sequence_id.encode()).hexdigest()[:32])
        sample_times = np.rint(np.arange(len(rgb)) / fs * 1e9).astype(np.int64)
        for start in range(0, len(rgb) - window_samples + 1, stride_samples):
            end = start + window_samples
            attempted += 1
            result = pipeline.process_window(
                rgb[start:end],
                sample_times[start:end],
                sample_rate_hz=fs,
                boot_id=boot_id,
                observation_quality=1.0,
            )
            if result.summary.hr.value is not None:
                predicted.append(float(result.summary.hr.value))
                reference.append(reference_bpm)
    accepted = len(predicted)
    coverage = accepted / attempted if attempted else 0.0
    if not predicted:
        metrics: tuple[float | None, ...] = (None, None, None, None, None, None)
    else:
        pred = np.asarray(predicted, dtype=np.float64)
        ref = np.asarray(reference, dtype=np.float64)
        errors = pred - ref
        mae_value = float(np.mean(np.abs(errors)))
        rmse_value = float(np.sqrt(np.mean(errors**2)))
        correlation_value = (
            float(np.corrcoef(pred, ref)[0, 1])
            if len(pred) >= 2 and float(np.std(pred)) > 0 and float(np.std(ref)) > 0
            else None
        )
        bias_value = float(np.mean(errors))
        error_sd = float(np.std(errors, ddof=1)) if len(errors) >= 2 else 0.0
        metrics = (
            mae_value,
            rmse_value,
            correlation_value,
            bias_value,
            bias_value - 1.96 * error_sd,
            bias_value + 1.96 * error_sd,
        )
    report_mae, report_rmse, report_correlation, report_bias, loa_lower, loa_upper = metrics
    return ReplayReport(
        dataset_name=manifest.dataset_name,
        dataset_version=manifest.dataset_version,
        split=split,
        subject_count=len({item.subject_id for item in selected}),
        sequence_count=len(selected),
        attempted_windows=attempted,
        accepted_windows=accepted,
        coverage=coverage,
        mae_bpm=report_mae,
        rmse_bpm=report_rmse,
        correlation=report_correlation,
        bias_bpm=report_bias,
        loa_lower_bpm=loa_lower,
        loa_upper_bpm=loa_upper,
        backend_name=backend.identity.name,
        backend_version=backend.identity.version,
        backend_sha256=backend.identity.implementation_sha256,
    )
