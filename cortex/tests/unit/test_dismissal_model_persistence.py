"""F21: dismissal-model weights persist across daemon restarts.

Each test fails on ``main`` (where weights live only on
``self._dismissal_model_weights`` and a fresh TriggerPolicy always starts
at zeros) and passes on this branch after F21's atomic-write persistence
lands.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from cortex.libs.config.settings import InterventionConfig
from cortex.services.state_engine.trigger_policy import (
    DISMISSAL_MODEL_VERSION,
    TriggerPolicy,
)


def _make_policy(path: Path) -> TriggerPolicy:
    """Construct a TriggerPolicy whose model file lives at ``path``."""
    return TriggerPolicy(
        config=InterventionConfig(),
        dismissal_model_path=path,
    )


def _force_flush_via_burst(policy: TriggerPolicy) -> None:
    """Drive enough updates to overshoot the debounce-by-count threshold."""
    for i in range(20):
        policy.record_outcome(
            dismissed=(i % 2 == 0),
            confidence=0.9,
            context_complexity=0.5,
            typing_burst_seconds=2.0,
        )


# ---------------------------------------------------------------------------
# Case 1: update + read-back
# ---------------------------------------------------------------------------


def test_update_persists_to_disk(tmp_path: Path) -> None:
    """After enough updates the model JSON appears on disk and matches state."""
    model_file = tmp_path / "dismissal_model.json"
    policy = _make_policy(model_file)

    _force_flush_via_burst(policy)

    assert model_file.exists(), "dismissal_model.json should be written after burst"
    data = json.loads(model_file.read_text())
    assert data["model_version"] == DISMISSAL_MODEL_VERSION
    weights = data["weights"]
    assert isinstance(weights, list) and len(weights) == 3
    # At least one weight must have moved off zero — the SGD step has run.
    assert any(abs(w) > 1e-9 for w in weights), (
        "Burst of outcomes should have moved the weights off the cold-start zeros"
    )
    # The in-memory weights match what we persisted.
    assert tuple(weights) == pytest.approx(policy._dismissal_model_weights)


# ---------------------------------------------------------------------------
# Case 2: restart rehydrates the model
# ---------------------------------------------------------------------------


def test_restart_rehydrates_weights(tmp_path: Path) -> None:
    """A second TriggerPolicy pointing at the same path picks up trained weights."""
    model_file = tmp_path / "dismissal_model.json"
    first = _make_policy(model_file)
    _force_flush_via_burst(first)
    trained_weights = first._dismissal_model_weights

    # Simulate daemon restart: drop the policy and rebuild from the same file.
    del first
    second = _make_policy(model_file)
    assert second._dismissal_model_weights == pytest.approx(trained_weights)
    # Outcome counter should also rehydrate so the predictor doesn't think
    # it's at cold-start.
    assert second._dismissal_outcomes >= 10


# ---------------------------------------------------------------------------
# Case 3: missing file → clean cold start
# ---------------------------------------------------------------------------


def test_missing_file_cold_starts(tmp_path: Path) -> None:
    """No model file → zeros, no crash, no spurious file creation on construct."""
    model_file = tmp_path / "does_not_exist.json"
    assert not model_file.exists()
    policy = _make_policy(model_file)
    assert policy._dismissal_model_weights == (0.0, 0.0, 0.0)
    assert policy._dismissal_outcomes == 0
    # Construction alone must NOT create the file.
    assert not model_file.exists()


# ---------------------------------------------------------------------------
# Case 4: version mismatch → cold start, keeps old file (caller decides)
# ---------------------------------------------------------------------------


def test_version_mismatch_cold_starts(tmp_path: Path) -> None:
    """A file with the wrong model_version is ignored; weights stay zero."""
    model_file = tmp_path / "dismissal_model.json"
    model_file.write_text(
        json.dumps(
            {
                "model_version": DISMISSAL_MODEL_VERSION + 1,
                "weights": [0.7, 0.4, 0.2],
                "outcomes": 50,
                "saved_at": 0.0,
            }
        )
    )
    policy = _make_policy(model_file)
    assert policy._dismissal_model_weights == (0.0, 0.0, 0.0)
    assert policy._dismissal_outcomes == 0


# ---------------------------------------------------------------------------
# Case 5: concurrent updates produce no torn write
# ---------------------------------------------------------------------------


def test_concurrent_updates_no_torn_write(tmp_path: Path) -> None:
    """Many threads calling record_outcome do not leave a half-written file.

    Each successful flush goes through atomic_write_json (tmp + os.replace),
    so even if multiple threads race the file should always be a complete,
    parseable JSON object — never truncated/empty.
    """
    model_file = tmp_path / "dismissal_model.json"
    policy = _make_policy(model_file)

    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for i in range(50):
                policy.record_outcome(
                    dismissed=(i % 3 == 0),
                    confidence=0.5 + (i % 5) * 0.1,
                    context_complexity=0.4,
                    typing_burst_seconds=1.0,
                )
        except BaseException as exc:  # pragma: no cover — defensive
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"worker threads raised: {errors}"

    # Force one final flush so we have a definite file to inspect.
    policy.flush_dismissal_model()
    assert model_file.exists()
    payload = model_file.read_text()
    # Must parse cleanly — a torn write would land us with truncated JSON.
    data = json.loads(payload)
    assert data["model_version"] == DISMISSAL_MODEL_VERSION
    assert len(data["weights"]) == 3
    assert all(isinstance(w, (int, float)) for w in data["weights"])
