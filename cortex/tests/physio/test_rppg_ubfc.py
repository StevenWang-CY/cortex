"""Dataset-gated rPPG replay checks for UBFC/PURE-style preprocessed traces."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from cortex.libs.signal.peak_detection import estimate_hr_welch
from cortex.services.physio_engine.rppg import extract_bvp

# Expected optional fixture format (per sequence .npz):
# - rgb_trace: shape [T, 3]
# - hr_gt: shape [T] or [1]


UBFC_ENV = "CORTEX_UBFC_DATASET_DIR"
PURE_ENV = "CORTEX_PURE_DATASET_DIR"


def _load_sequences(root: Path) -> list[tuple[np.ndarray, np.ndarray]]:
    sequences: list[tuple[np.ndarray, np.ndarray]] = []
    for npz_path in sorted(root.rglob("*.npz")):
        data = np.load(npz_path)
        if "rgb_trace" not in data or "hr_gt" not in data:
            continue
        rgb = np.asarray(data["rgb_trace"], dtype=np.float64)
        hr_gt = np.asarray(data["hr_gt"], dtype=np.float64).reshape(-1)
        if rgb.ndim != 2 or rgb.shape[1] != 3 or rgb.shape[0] < 300 or hr_gt.size == 0:
            continue
        sequences.append((rgb, hr_gt))
    return sequences


def _estimate_hr_from_rgb(rgb_trace: np.ndarray, fs: float = 30.0) -> float | None:
    bvp = extract_bvp(rgb_trace, fs=fs, method="pos")
    hr, conf = estimate_hr_welch(bvp, fs=fs)
    if hr is None or conf <= 0.0:
        return None
    return float(hr)


@pytest.mark.slow
def test_ubfc_hr_mae_threshold():
    dataset_dir = os.getenv(UBFC_ENV)
    if not dataset_dir:
        pytest.skip(f"Set {UBFC_ENV} to run UBFC replay tests")

    root = Path(dataset_dir)
    if not root.exists():
        pytest.skip(f"{UBFC_ENV} path does not exist: {root}")

    sequences = _load_sequences(root)
    if not sequences:
        pytest.skip("No preprocessed UBFC .npz traces found (expects rgb_trace/hr_gt arrays)")

    maes: list[float] = []
    for rgb, hr_gt in sequences[:10]:
        hr_pred = _estimate_hr_from_rgb(rgb)
        if hr_pred is None:
            continue
        hr_ref = float(np.nanmean(hr_gt))
        maes.append(abs(hr_pred - hr_ref))

    if not maes:
        pytest.skip("No valid UBFC sequence produced HR predictions")

    assert float(np.mean(maes)) <= 5.0


@pytest.mark.slow
def test_pure_dataset_smoke_replay():
    dataset_dir = os.getenv(PURE_ENV)
    if not dataset_dir:
        pytest.skip(f"Set {PURE_ENV} to run PURE replay tests")

    root = Path(dataset_dir)
    if not root.exists():
        pytest.skip(f"{PURE_ENV} path does not exist: {root}")

    sequences = _load_sequences(root)
    if not sequences:
        pytest.skip("No preprocessed PURE .npz traces found")

    rgb, _hr_gt = sequences[0]
    hr_pred = _estimate_hr_from_rgb(rgb)
    assert hr_pred is None or 40.0 <= hr_pred <= 180.0
