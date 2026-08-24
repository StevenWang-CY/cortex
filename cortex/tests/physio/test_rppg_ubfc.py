"""Optional checksum-verified, subject-disjoint public-dataset replay gates."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cortex.services.physio_engine.v2.replay import evaluate_dataset_manifest

UBFC_ENV = "CORTEX_UBFC_DATASET_MANIFEST"
PURE_ENV = "CORTEX_PURE_DATASET_MANIFEST"


def _manifest_from_env(variable: str) -> Path:
    configured = os.getenv(variable)
    if not configured:
        pytest.skip(f"Set {variable} to a v1 checksum-bearing replay manifest")
    path = Path(configured)
    if not path.is_file():
        pytest.skip(f"{variable} manifest does not exist: {path}")
    return path


@pytest.mark.slow
def test_ubfc_subject_disjoint_hr_gate() -> None:
    report = evaluate_dataset_manifest(_manifest_from_env(UBFC_ENV))
    assert report.subject_count >= 1
    assert report.attempted_windows >= 1
    assert report.coverage >= 0.80
    assert report.mae_bpm is not None and report.mae_bpm <= 5.0
    assert report.rmse_bpm is not None
    assert report.bias_bpm is not None
    assert report.loa_lower_bpm is not None
    assert report.loa_upper_bpm is not None


@pytest.mark.slow
def test_pure_subject_disjoint_smoke_replay() -> None:
    report = evaluate_dataset_manifest(_manifest_from_env(PURE_ENV))
    assert report.subject_count >= 1
    assert report.attempted_windows >= 1
    assert 0.0 <= report.coverage <= 1.0
    assert report.backend_name == "pos"
