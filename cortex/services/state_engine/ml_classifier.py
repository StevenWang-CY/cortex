"""
State Engine — Optional Per-User ML Classifier

Lightweight NumPy logistic model with optional Platt scaling.
No external ML dependencies are required.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass
class PlattCalibrator:
    """Binary logistic calibrator over raw model logits."""

    a: float = 1.0
    b: float = 0.0

    def fit(self, logits: np.ndarray, labels: np.ndarray, epochs: int = 200, lr: float = 0.05) -> None:
        if logits.size == 0:
            return
        a = float(self.a)
        b = float(self.b)
        y = labels.astype(np.float64)
        x = logits.astype(np.float64)
        for _ in range(epochs):
            z = a * x + b
            p = 1.0 / (1.0 + np.exp(-np.clip(z, -20.0, 20.0)))
            da = float(np.mean((p - y) * x))
            db = float(np.mean(p - y))
            a -= lr * da
            b -= lr * db
        self.a = a
        self.b = b

    def predict(self, logits: np.ndarray) -> np.ndarray:
        z = self.a * logits + self.b
        return 1.0 / (1.0 + np.exp(-np.clip(z, -20.0, 20.0)))


class PerUserLogisticClassifier:
    """Simple logistic classifier for `P(HYPER | features)`."""

    def __init__(self, n_features: int, l2: float = 1e-3) -> None:
        self.n_features = n_features
        self.l2 = l2
        self.weights = np.zeros(n_features, dtype=np.float64)
        self.bias = 0.0
        self.calibrator = PlattCalibrator()
        self.is_fitted = False

    def fit(
        self,
        x: np.ndarray,
        y: np.ndarray,
        *,
        epochs: int = 400,
        lr: float = 0.05,
        calibrate: bool = True,
    ) -> None:
        if x.ndim != 2:
            raise ValueError("x must be 2D")
        if x.shape[1] != self.n_features:
            raise ValueError("feature dimension mismatch")
        if y.ndim != 1 or y.shape[0] != x.shape[0]:
            raise ValueError("y must be 1D and aligned with x")
        if x.shape[0] == 0:
            return

        w = self.weights.copy()
        b = float(self.bias)
        yv = y.astype(np.float64)
        xv = x.astype(np.float64)

        for _ in range(epochs):
            logits = xv @ w + b
            p = 1.0 / (1.0 + np.exp(-np.clip(logits, -20.0, 20.0)))
            grad_w = (xv.T @ (p - yv)) / x.shape[0] + self.l2 * w
            grad_b = float(np.mean(p - yv))
            w -= lr * grad_w
            b -= lr * grad_b

        self.weights = w
        self.bias = b
        self.is_fitted = True

        if calibrate:
            logits = xv @ self.weights + self.bias
            self.calibrator.fit(logits, yv)

    def predict_proba(self, x: np.ndarray) -> np.ndarray:
        if x.ndim == 1:
            x = x.reshape(1, -1)
        logits = x.astype(np.float64) @ self.weights + self.bias
        if self.is_fitted:
            return self.calibrator.predict(logits)
        return 1.0 / (1.0 + np.exp(-np.clip(logits, -20.0, 20.0)))

    def to_dict(self) -> dict:
        return {
            "n_features": self.n_features,
            "l2": self.l2,
            "weights": self.weights.tolist(),
            "bias": self.bias,
            "calibrator": {"a": self.calibrator.a, "b": self.calibrator.b},
            "is_fitted": self.is_fitted,
        }

    @classmethod
    def from_dict(cls, data: dict) -> PerUserLogisticClassifier:
        n_features = int(data.get("n_features", 0))
        model = cls(n_features=n_features, l2=float(data.get("l2", 1e-3)))
        model.weights = np.asarray(data.get("weights", [0.0] * n_features), dtype=np.float64)
        model.bias = float(data.get("bias", 0.0))
        cal = data.get("calibrator", {})
        model.calibrator = PlattCalibrator(a=float(cal.get("a", 1.0)), b=float(cal.get("b", 0.0)))
        model.is_fitted = bool(data.get("is_fitted", False))
        return model

    def save(self, path: str | Path) -> None:
        payload = self.to_dict()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> PerUserLogisticClassifier:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data)
