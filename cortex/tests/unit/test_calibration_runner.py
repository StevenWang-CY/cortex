"""Calibration protocol, provenance, and commit-boundary tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from cortex.application.clock import FakeClock
from cortex.libs.config.settings import CortexConfig, get_config
from cortex.libs.schemas.calibration import (
    CalibrationMetricMaturity,
    CalibrationMetricName,
    CalibrationProvenance,
)
from cortex.libs.schemas.observations import CameraIdentity
from cortex.libs.schemas.physiology import SignalAlgorithmIdentity
from cortex.services.capture_service import calibration_runner as runner_module
from cortex.services.capture_service.calibration_runner import (
    CalibrationCapture,
    CalibrationCaptureUnavailable,
    CalibrationProgress,
    CalibrationRunner,
    compute_baselines,
)
from cortex.services.capture_service.calibration_store import CalibrationProfileStore


@pytest.fixture()
def config(tmp_path: Path) -> CortexConfig:
    value = get_config().model_copy(deep=True)
    value.storage.path = str(tmp_path)
    return value


def _algorithm(name: str) -> SignalAlgorithmIdentity:
    return SignalAlgorithmIdentity(
        name=name,
        version="2.0.0",
        implementation_sha256="a" * 64,
        configuration_sha256="b" * 64,
        selection_mode="fixed",
    )


def _measured_capture() -> CalibrationCapture:
    samples = runner_module._empty_samples()  # noqa: SLF001 - fixture boundary
    samples["heart_rate_rest"].extend([69.0, 70.0, 71.0])
    samples["respiration_rate_rest"].extend([14.0, 15.0, 16.0])
    samples["blink_rate_work"].extend([14.0, 15.0, 16.0])
    samples["open_eye_ratio_work"].extend([0.30, 0.31, 0.32])
    samples["mouse_velocity_work"].extend([400.0, 500.0, 600.0])
    samples["mouse_variance_work"].extend([8_000.0, 10_000.0, 12_000.0])
    samples["neutral_head_pitch"].extend([1.0, 2.0, 3.0])
    samples["neutral_face_scale"].extend([178.0, 180.0, 182.0])
    samples["quality"].extend([0.7, 0.8, 0.9])
    return CalibrationCapture(
        samples=samples,
        camera=CameraIdentity(
            identity_key="builtin:facetime-hd",
            device_id=0,
            device_name="FaceTime HD Camera",
            source="builtin",
            width=1280,
            height=720,
        ),
        phase_valid_duration_seconds={
            "camera_quality_check": 12.0,
            "physiological_rest": 54.0,
            "representative_work": 54.0,
        },
        phase_scheduled_count={
            "camera_quality_check": 360,
            "physiological_rest": 1_620,
            "representative_work": 1_620,
        },
        phase_valid_count={
            "camera_quality_check": 350,
            "physiological_rest": 1_550,
            "representative_work": 1_500,
        },
        phase_quality={
            "camera_quality_check": [0.7, 0.8, 0.9],
            "physiological_rest": [0.7, 0.8, 0.9],
            "representative_work": [0.7, 0.8, 0.9],
        },
        algorithms={
            "physiology": _algorithm("pulse-v2"),
            "blink": _algorithm("blink-v2"),
            "head_pose": _algorithm("head-pose-v2"),
            "telemetry": _algorithm("telemetry-v2"),
        },
    )


def test_simulation_can_only_create_isolated_demo_profile(config: CortexConfig) -> None:
    runner = CalibrationRunner(duration_seconds=1, simulate=True, config=config)
    asyncio.run(runner.start())
    preview = runner.preview_profile()
    assert preview.provenance == CalibrationProvenance.DEMO.value
    assert preview.approved_at_unix_ms is None

    with pytest.raises(RuntimeError, match="demo-only"):
        asyncio.run(runner.finish(approved_by_user=True))
    demo = asyncio.run(runner.save_demo())
    store = CalibrationProfileStore(config.storage.path)
    assert store.profile_path(demo.profile_id, demo=True).exists()
    assert store.load_active() is None
    assert not (Path(config.storage.path) / "baselines" / "default.json").exists()


def test_capture_emits_review_before_commit_and_completion_after_commit(
    config: CortexConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _collect(*_args, **_kwargs) -> CalibrationCapture:
        return _measured_capture()

    monkeypatch.setattr(runner_module, "_collect_live_calibration", _collect)
    progress: list[CalibrationProgress] = []
    runner = CalibrationRunner(
        duration_seconds=120,
        config=config,
        clock=FakeClock(wall_unix_ms=1_700_000_000_000),
    )
    asyncio.run(runner.start(on_progress=progress.append))
    assert progress[-1].status == "review_required"
    assert all(item.status != "completed" for item in progress)

    profile = asyncio.run(runner.finish(approved_by_user=True))
    assert progress[-1].status == "applying"
    assert profile.provenance == CalibrationProvenance.MEASURED.value
    assert profile.approved_at_unix_ms is not None
    store = CalibrationProfileStore(config.storage.path)
    assert store.load_active() is None
    assert store.load_profile(profile.profile_id) == profile
    assert runner.is_committed_profile_active(str(profile.profile_id)) is False
    runner.activate_for_next_start(str(profile.profile_id))
    assert store.load_active() == profile
    assert runner.is_committed_profile_active(str(profile.profile_id)) is True
    assert runner.is_committed_profile_active(str(uuid4())) is False
    runner.mark_applied(str(profile.profile_id))
    assert progress[-1].status == "completed"


def test_failed_live_apply_leaves_staged_profile_inactive(
    config: CortexConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _collect(*_args, **_kwargs) -> CalibrationCapture:
        return _measured_capture()

    monkeypatch.setattr(runner_module, "_collect_live_calibration", _collect)
    progress: list[CalibrationProgress] = []
    runner = CalibrationRunner(duration_seconds=120, config=config)
    asyncio.run(runner.start(on_progress=progress.append))
    profile = asyncio.run(runner.finish(approved_by_user=True))
    runner.mark_failed("candidate was incompatible")

    store = CalibrationProfileStore(config.storage.path)
    assert store.load_profile(profile.profile_id) == profile
    assert store.load_active() is None
    assert progress[-1].status == "failed"
    assert "not confirmed" in progress[-1].phase_instruction


def test_explicit_approval_is_required(
    config: CortexConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _collect(*_args, **_kwargs) -> CalibrationCapture:
        return _measured_capture()

    monkeypatch.setattr(runner_module, "_collect_live_calibration", _collect)
    runner = CalibrationRunner(duration_seconds=120, config=config)
    asyncio.run(runner.start())
    with pytest.raises(ValueError, match="explicit user approval"):
        asyncio.run(runner.finish(approved_by_user=False))
    assert CalibrationProfileStore(config.storage.path).load_active() is None


def test_preview_separates_rest_work_and_experimental_maturity(
    config: CortexConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _collect(*_args, **_kwargs) -> CalibrationCapture:
        return _measured_capture()

    monkeypatch.setattr(runner_module, "_collect_live_calibration", _collect)
    runner = CalibrationRunner(duration_seconds=120, config=config)
    asyncio.run(runner.start())
    profile = runner.preview_profile()
    heart = profile.metric(CalibrationMetricName.HEART_RATE_BPM)
    blink = profile.metric(CalibrationMetricName.BLINK_RATE_PER_MIN)
    mouse = profile.metric(CalibrationMetricName.MOUSE_VELOCITY_PX_PER_S)
    assert heart is not None and heart.maturity == CalibrationMetricMaturity.EXPERIMENTAL.value
    assert blink is not None and blink.reference_task == "representative_work"
    assert mouse is not None and mouse.reference_task == "representative_work"
    assert heart.effective_sample_count <= heart.sample_count


def test_abort_never_writes_profile(config: CortexConfig) -> None:
    runner = CalibrationRunner(duration_seconds=10, simulate=True, config=config)

    async def _drive() -> None:
        async def _abort() -> None:
            await asyncio.sleep(0.1)
            runner.abort()

        await asyncio.gather(runner.start(), _abort())

    asyncio.run(_drive())
    assert runner.last_progress is not None
    assert runner.last_progress.status == "aborted"
    with pytest.raises(RuntimeError, match="aborted"):
        runner.preview_profile()
    assert CalibrationProfileStore(config.storage.path).load_active() is None


def test_live_unavailability_does_not_fall_back_to_synthetic(
    config: CortexConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _unavailable(*_args, **_kwargs) -> CalibrationCapture:
        raise CalibrationCaptureUnavailable("camera unavailable")

    monkeypatch.setattr(runner_module, "_collect_live_calibration", _unavailable)
    runner = CalibrationRunner(duration_seconds=1, config=config)
    with pytest.raises(CalibrationCaptureUnavailable):
        asyncio.run(runner.start())
    assert runner.used_simulation is False
    assert CalibrationProfileStore(config.storage.path).load_active() is None


def test_progress_reports_all_protocol_phases(config: CortexConfig) -> None:
    received: list[CalibrationProgress] = []
    runner = CalibrationRunner(duration_seconds=2, simulate=True, config=config)
    asyncio.run(runner.start(on_progress=received.append))
    phases = {item.phase for item in received}
    assert {
        "camera_quality_check",
        "physiological_rest",
        "representative_work",
        "review",
    } <= phases
    assert all(item.current_hrv is None for item in received)
    assert received[-1].status == "review_required"


def test_legacy_baseline_view_does_not_invent_missing_samples() -> None:
    empty = compute_baselines({})
    assert empty.is_calibrated is False
    samples = runner_module._empty_samples()  # noqa: SLF001 - compatibility fixture
    samples["blink_rate_work"].extend([12.0, 14.0])
    measured = compute_baselines(samples, clock=FakeClock(wall_unix_ms=1_000))
    assert measured.blink_rate_baseline == pytest.approx(13.0)
    assert measured.hr_baseline == 72.0
    assert measured.is_calibrated is True


def test_defensive_lifecycle_errors(config: CortexConfig) -> None:
    runner = CalibrationRunner(duration_seconds=1, simulate=True, config=config)
    with pytest.raises(RuntimeError, match="no completed capture"):
        runner.preview_profile()
    asyncio.run(runner.start())
    with pytest.raises(RuntimeError, match="already called"):
        asyncio.run(runner.start())
    with pytest.raises(ValueError):
        CalibrationRunner(duration_seconds=0, config=config)
