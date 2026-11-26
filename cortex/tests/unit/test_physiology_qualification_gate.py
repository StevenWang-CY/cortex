"""The simulated qualification must be able to fail.

``make verify`` runs ``qualify_physiology synthetic --check``.  A check that
always exits 0 would let an estimator change ship with a metric that no
longer meets its criterion, so the gate's own decision is pinned here.  The
simulation itself (about two minutes) is not run in the unit suite.
"""

from __future__ import annotations

from typing import Any

import pytest

from cortex.scripts import qualify_physiology
from cortex.scripts.qualify_physiology import (
    ADMITTED_METRICS,
    synthetic_gate_failures,
)
from cortex.services.eval.physio_qualification import METRIC_RMSSD


def _passing_run() -> dict[str, Any]:
    metrics: dict[str, Any] = {
        metric: {"qualified": True, "reasons": []} for metric in ADMITTED_METRICS
    }
    # RMSSD is reported and does not qualify; that must not fail the gate.
    metrics[METRIC_RMSSD] = {"qualified": False, "reasons": ["mae 12.6 > 12.0"]}
    clean = dict.fromkeys((*ADMITTED_METRICS, METRIC_RMSSD), 0.0)
    return {
        "metrics": metrics,
        "false_publication_rate": {
            "white": dict(clean),
            "drift": dict(clean),
            "pink": dict(clean),
        },
    }


def test_rmssd_is_not_an_admitted_metric() -> None:
    assert METRIC_RMSSD not in ADMITTED_METRICS
    assert set(ADMITTED_METRICS) == {
        "heart_rate_bpm",
        "sdnn_ms",
        "respiration_rate_bpm",
    }


def test_a_run_that_meets_every_admitted_criterion_passes() -> None:
    assert synthetic_gate_failures(_passing_run()) == []


@pytest.mark.parametrize("metric", ADMITTED_METRICS)
def test_an_admitted_metric_below_its_criterion_fails_and_is_named(metric: str) -> None:
    run = _passing_run()
    run["metrics"][metric] = {"qualified": False, "reasons": ["mae 9.0 > 3.0"]}

    failures = synthetic_gate_failures(run)

    assert len(failures) == 1
    assert metric in failures[0]
    assert "mae 9.0 > 3.0" in failures[0]


@pytest.mark.parametrize("metric", ADMITTED_METRICS)
def test_a_missing_admitted_metric_fails(metric: str) -> None:
    run = _passing_run()
    del run["metrics"][metric]

    failures = synthetic_gate_failures(run)

    assert failures == [f"{metric}: no result"]


@pytest.mark.parametrize("metric", (*ADMITTED_METRICS, METRIC_RMSSD))
def test_any_value_published_without_a_pulse_fails(metric: str) -> None:
    run = _passing_run()
    run["false_publication_rate"]["drift"][metric] = 0.004

    failures = synthetic_gate_failures(run)

    assert len(failures) == 1
    assert metric in failures[0]
    assert "drift" in failures[0]


def test_check_mode_exit_code_follows_the_gate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(qualify_physiology, "run_synthetic", _passing_run)
    assert qualify_physiology.main(["synthetic", "--check"]) == 0
    assert "meet their criteria" in capsys.readouterr().out

    def failing_run() -> dict[str, Any]:
        run = _passing_run()
        run["metrics"]["sdnn_ms"] = {"qualified": False, "reasons": ["r 0.41 < 0.80"]}
        return run

    monkeypatch.setattr(qualify_physiology, "run_synthetic", failing_run)
    assert qualify_physiology.main(["synthetic", "--check"]) == 1
    assert "sdnn_ms: r 0.41 < 0.80" in capsys.readouterr().err


def test_without_check_a_failing_run_still_reports_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def failing_run() -> dict[str, Any]:
        run = _passing_run()
        run["metrics"]["heart_rate_bpm"] = {"qualified": False, "reasons": ["x"]}
        return run

    monkeypatch.setattr(qualify_physiology, "run_synthetic", failing_run)
    assert qualify_physiology.main(["synthetic"]) == 0
    assert '"mode": "synthetic"' in capsys.readouterr().out
