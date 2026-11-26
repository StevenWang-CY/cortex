"""Daemon wiring of camera physiology: reference tracking, strain breaks, processor ownership.

Runs against the real :class:`CortexDaemon` without starting capture, the
WebSocket server or the state loop; only the collaborators a method touches
are stubbed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from cortex.libs.schemas.features import FeatureName, FeatureValue, FeatureVector
from cortex.libs.schemas.observations import MissingReason
from cortex.libs.schemas.state import UserBaselines
from cortex.libs.schemas.ws_message_types import MessageType
from cortex.services.physio_engine.v2.engine import PhysiologyEngineV2
from cortex.services.state_engine.feature_schema import FEATURE_DEFINITIONS
from cortex.services.state_engine.physiology_baseline import (
    PhysiologyBaselineTracker,
    PhysiologyMetricName,
    PhysiologyReference,
)
from cortex.services.state_engine.strain_load import StrainLoadTracker


@pytest.fixture()
def isolated_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    storage = tmp_path / "storage"
    storage.mkdir()
    monkeypatch.setenv("CORTEX_STORAGE__PATH", str(storage))
    from cortex.libs.config import settings as settings_mod

    if hasattr(settings_mod.get_config, "cache_clear"):
        settings_mod.get_config.cache_clear()
    return storage


@pytest.fixture()
def daemon(isolated_storage: Path) -> Any:
    from cortex.libs.config.settings import get_config
    from cortex.services.runtime_daemon import CortexDaemon

    # A private copy: these tests flip intervention switches.
    return CortexDaemon(config=get_config().model_copy(deep=True))


def _valid(name: FeatureName, value: float) -> FeatureValue:
    return FeatureValue(
        value=value,
        valid=True,
        quality=0.9,
        age_ms=0,
        source_window_ms=FEATURE_DEFINITIONS[name].source_window_ms,
        algorithm_version="test-v1",
    )


def _invalid(name: FeatureName) -> FeatureValue:
    return FeatureValue(
        valid=False,
        quality=0.0,
        age_ms=0,
        source_window_ms=FEATURE_DEFINITIONS[name].source_window_ms,
        algorithm_version="test-v1",
        missing_reason=MissingReason.NO_FACE,
    )


def _vector(features: dict[FeatureName, FeatureValue], **flat: Any) -> FeatureVector:
    return FeatureVector(timestamp=1.0, telemetry_seen_count=5, features=features, **flat)


# ---------------------------------------------------------------------------
# Personal reference
# ---------------------------------------------------------------------------


def test_reference_tracker_sees_only_valid_measurements_and_scorer_gets_the_reference(
    daemon: Any,
) -> None:
    tracker = MagicMock(spec=PhysiologyBaselineTracker)
    reference = PhysiologyReference()
    tracker.reference.return_value = reference
    daemon._physiology_baseline = tracker
    daemon._scorer.set_physiology_reference = MagicMock()

    vector = _vector(
        {
            FeatureName.HEART_RATE_BPM: _valid(FeatureName.HEART_RATE_BPM, 74.0),
            FeatureName.HRV_SDNN_MS: _invalid(FeatureName.HRV_SDNN_MS),
            FeatureName.RESPIRATION_RATE_BPM: _valid(FeatureName.RESPIRATION_RATE_BPM, 15.0),
            # Blink rate absent entirely; eye closure is not a referenced metric.
            FeatureName.PERCLOS: _valid(FeatureName.PERCLOS, 0.1),
            FeatureName.KEYPRESS_RATE_PER_MIN: _valid(FeatureName.KEYPRESS_RATE_PER_MIN, 60.0),
        }
    )
    returned = daemon._observe_physiology_reference(vector, 123.0)

    observed = [call.args for call in tracker.observe.call_args_list]
    assert observed == [
        (PhysiologyMetricName.HEART_RATE, 74.0, 123.0),
        (PhysiologyMetricName.RESPIRATION, 15.0, 123.0),
    ]
    assert returned is reference
    daemon._scorer.set_physiology_reference.assert_called_once_with(reference)


def test_session_warm_up_reaches_the_scorer(daemon: Any) -> None:
    """Valid heart rates fed tick by tick become a ready reference the scorer reads."""

    reference = PhysiologyReference()
    for tick in range(60):
        reference = daemon._observe_physiology_reference(
            _vector({FeatureName.HEART_RATE_BPM: _valid(FeatureName.HEART_RATE_BPM, 72.0)}),
            1_000.0 + 2.5 * tick,
        )
    assert reference.heart_rate.ready
    assert reference.heart_rate.center == pytest.approx(72.0)
    assert not reference.sdnn.ready
    assert daemon._scorer._physiology_reference == reference
    # One tracker for the session, not one per tick.
    assert daemon._physiology_baseline_tracker() is daemon._physiology_baseline_tracker()


def test_a_physical_camera_change_discards_the_personal_reference(daemon: Any) -> None:
    """The reference describes one person *and one camera* (physiology_baseline docstring).

    When the daemon resets camera-dependent state because the physical camera
    changed, a reference learned from the previous camera's bias must not be
    carried over to the new one.
    """

    for tick in range(60):
        daemon._observe_physiology_reference(
            _vector({FeatureName.HEART_RATE_BPM: _valid(FeatureName.HEART_RATE_BPM, 72.0)}),
            2.5 * tick,
        )
    assert daemon._physiology_baseline_tracker().reference().heart_rate.ready
    daemon._reset_camera_dependent_state(invalidate_calibration=True)
    assert not daemon._physiology_baseline_tracker().reference().heart_rate.ready


# ---------------------------------------------------------------------------
# Strain break suggestion
# ---------------------------------------------------------------------------


def _crossed_tracker(threshold: float = 600.0) -> StrainLoadTracker:
    tracker = StrainLoadTracker(threshold_seconds=threshold)
    tracker.update(1.0, 0.0)
    now = 0.0
    while tracker.current_load < tracker.threshold:
        now += 1.0
        tracker.update(1.0, now)
    return tracker


def _stub_presentation(daemon: Any, *, allowed: bool = True) -> None:
    daemon._ws_server.send_message = AsyncMock(return_value=True)
    daemon._trigger_policy.record_intervention = MagicMock()
    daemon._interruption_allowed = MagicMock(return_value=allowed)


async def test_crossed_threshold_sends_exactly_one_strain_break(daemon: Any) -> None:
    _stub_presentation(daemon)
    tracker = _crossed_tracker()
    vector = _vector({})

    await daemon._maybe_recommend_strain_break(tracker, vector, 600.0)
    await daemon._maybe_recommend_strain_break(tracker, vector, 601.0)

    daemon._ws_server.send_message.assert_awaited_once()
    message_type, payload = daemon._ws_server.send_message.await_args.args
    assert message_type == MessageType.BREAK_RECOMMENDATION.value
    assert payload["basis"] == "physiological_strain"
    assert payload["stress_load"] == pytest.approx(tracker.current_load)
    assert payload["threshold"] == pytest.approx(tracker.threshold)
    assert payload["duration_seconds"] == daemon.config.intervention.strain_break_duration_seconds
    daemon._trigger_policy.record_intervention.assert_called_once_with(timestamp=600.0)
    daemon._interruption_allowed.assert_called_once_with(
        surface="strain_break", current_time=600.0
    )


async def test_refused_strain_break_is_deferred_not_consumed(daemon: Any) -> None:
    _stub_presentation(daemon, allowed=False)
    tracker = _crossed_tracker()

    await daemon._maybe_recommend_strain_break(tracker, _vector({}), 600.0)
    daemon._ws_server.send_message.assert_not_awaited()
    daemon._trigger_policy.record_intervention.assert_not_called()

    # The crossing is still owed and is delivered once the gate opens.
    daemon._interruption_allowed.return_value = True
    await daemon._maybe_recommend_strain_break(tracker, _vector({}), 700.0)
    daemon._ws_server.send_message.assert_awaited_once()
    daemon._trigger_policy.record_intervention.assert_called_once_with(timestamp=700.0)


async def test_disabled_strain_breaks_send_nothing(daemon: Any) -> None:
    _stub_presentation(daemon)
    daemon.config.intervention.enable_strain_break_suggestions = False
    tracker = _crossed_tracker()

    await daemon._maybe_recommend_strain_break(tracker, _vector({}), 600.0)

    daemon._ws_server.send_message.assert_not_awaited()
    daemon._trigger_policy.record_intervention.assert_not_called()
    daemon._interruption_allowed.assert_not_called()


async def test_below_threshold_sends_nothing(daemon: Any) -> None:
    _stub_presentation(daemon)
    tracker = StrainLoadTracker()
    tracker.update(1.0, 0.0)
    tracker.update(1.0, 30.0)
    await daemon._maybe_recommend_strain_break(tracker, _vector({}), 30.0)
    daemon._ws_server.send_message.assert_not_awaited()
    daemon._interruption_allowed.assert_not_called()


@pytest.mark.parametrize(("sdnn", "pattern"), [(30.0, "coherent"), (50.0, "box"), (None, "box")])
async def test_breathing_pattern_follows_variability_against_the_reference(
    daemon: Any, sdnn: float | None, pattern: str
) -> None:
    _stub_presentation(daemon)
    daemon._physiology_baseline = PhysiologyBaselineTracker(
        UserBaselines(
            calibrated_at=datetime(2026, 11, 1, tzinfo=UTC),
            metric_distributions={"hrv_sdnn_ms": {"mu": 50.0, "sigma": 10.0}},
        )
    )
    await daemon._maybe_recommend_strain_break(
        _crossed_tracker(), _vector({}, hrv_sdnn=sdnn), 600.0
    )
    _, payload = daemon._ws_server.send_message.await_args.args
    assert payload["breathing_pattern"] == pattern


def test_strain_tracker_uses_the_configured_threshold_and_half_life(daemon: Any) -> None:
    daemon.config.intervention.strain_break_threshold_seconds = 900.0
    daemon.config.intervention.strain_recovery_half_life_seconds = 300.0
    tracker = daemon._strain_load_tracker()
    assert tracker is daemon._strain_load_tracker()
    assert tracker.threshold == pytest.approx(900.0)
    tracker.update(1.0, 0.0)
    tracker.update(1.0, 30.0)
    load = tracker.current_load
    for second in range(31, 331):
        tracker.update(0.0, float(second))
    assert tracker.current_load == pytest.approx(0.5 * load)


# ---------------------------------------------------------------------------
# Processor ownership
# ---------------------------------------------------------------------------


def test_camera_physiology_processor_follows_the_engine(daemon: Any) -> None:
    first = daemon._camera_physiology()
    assert first is daemon._camera_physiology()
    assert first.engine is daemon._physiology_v2

    replacement = PhysiologyEngineV2(daemon.config.signal.rppg)
    daemon._physiology_v2 = replacement
    rebuilt = daemon._camera_physiology()
    assert rebuilt is not first
    assert rebuilt.engine is replacement
    assert daemon._camera_physiology() is rebuilt
