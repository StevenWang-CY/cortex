"""Calibration-oriented tests for the optional per-user ML classifier."""

from __future__ import annotations

import numpy as np

from cortex.services.state_engine.ml_classifier import PerUserLogisticClassifier


def _reliability_gap(y_true: np.ndarray, y_prob: np.ndarray, bins: int = 10) -> float:
    edges = np.linspace(0.0, 1.0, bins + 1)
    gaps = []
    for i in range(bins):
        lo, hi = edges[i], edges[i + 1]
        mask = (y_prob >= lo) & (y_prob < hi)
        if not np.any(mask):
            continue
        pred = float(np.mean(y_prob[mask]))
        obs = float(np.mean(y_true[mask]))
        gaps.append(abs(pred - obs))
    return float(np.mean(gaps)) if gaps else 0.0


def test_ml_classifier_brier_and_reliability_smoke():
    rng = np.random.default_rng(101)
    n = 600
    x = rng.normal(0.0, 1.0, size=(n, 8))

    logits = 1.3 * x[:, 0] - 0.8 * x[:, 1] + 0.5 * x[:, 2] - 0.4
    p = 1.0 / (1.0 + np.exp(-np.clip(logits, -20.0, 20.0)))
    y = rng.binomial(1, p).astype(np.float64)

    split = 450
    model = PerUserLogisticClassifier(n_features=8)
    model.fit(x[:split], y[:split], epochs=500, lr=0.05, calibrate=True)

    prob = model.predict_proba(x[split:]).reshape(-1)
    y_test = y[split:]

    brier = float(np.mean((prob - y_test) ** 2))
    rel_gap = _reliability_gap(y_test, prob, bins=8)

    assert brier < 0.23
    assert rel_gap < 0.20
