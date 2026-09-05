"""Atomic calibration activation and transport-parity contracts."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from cortex.application.clock import FakeClock
from cortex.libs.config.settings import CortexConfig, get_config
from cortex.libs.schemas.calibration import (
    CalibrationBaselineValues,
    CalibrationCameraIdentity,
    CalibrationDistribution,
    CalibrationMetricMaturity,
    CalibrationMetricName,
    CalibrationMetricSummary,
    CalibrationProfile,
    CalibrationProvenance,
    CalibrationReferenceTask,
)
from cortex.libs.schemas.observations import (
    CameraIdentity,
    ObservationEnvelope,
    ObservationSource,
    ObservationValidity,
)
from cortex.libs.schemas.physiology import SignalAlgorithmIdentity
from cortex.libs.schemas.state import UserBaselines
from cortex.libs.schemas.ws_message import WSMessage
from cortex.libs.schemas.ws_message_types import MessageType
from cortex.services.api_gateway.app import registry
from cortex.services.api_gateway.websocket_server import WebSocketClient
from cortex.services.capture_service.calibration_store import (
    CalibrationProfileStore,
    calibration_profile_sha256,
)
from cortex.services.capture_service.feature_factory import (
    production_calibration_algorithm_identities,
)
from cortex.services.runtime_daemon import CortexDaemon

_PROFILE_ID = UUID("00000000-0000-0000-0000-000000000401")
_EVENT_ID = UUID("00000000-0000-0000-0000-000000000402")
_BOOT_ID = UUID("00000000-0000-0000-0000-000000000403")


def _config(root: Path) -> CortexConfig:
    config = get_config().model_copy(deep=True)
    config.storage.path = str(root)
    return config


def _metric(
    name: CalibrationMetricName,
    value: float,
    unit: str,
    task: CalibrationReferenceTask,
    algorithm: SignalAlgorithmIdentity,
) -> CalibrationMetricSummary:
    return CalibrationMetricSummary(
        metric=name,
        unit=unit,
        reference_task=task,
        maturity=CalibrationMetricMaturity.OBSERVED,
        value=value,
        distribution=CalibrationDistribution(
            mean=value,
            std=0.5,
            p10=value - 0.5,
            median=value,
            p90=value + 0.5,
        ),
        sample_count=60,
        effective_sample_count=20.0,
        valid_duration_seconds=40.0,
        missing_fraction=0.05,
        quality_p10=0.6,
        quality_median=0.8,
        quality_p90=0.9,
        algorithm=algorithm,
    )


def _profile(
    config: CortexConfig,
    *,
    profile_id: UUID = _PROFILE_ID,
    feature_schema_version: str = "features/2.0",
    camera_key: str = "builtin:facetime-hd",
) -> CalibrationProfile:
    algorithms = production_calibration_algorithm_identities(config)
    return CalibrationProfile(
        profile_id=profile_id,
        provenance=CalibrationProvenance.MEASURED,
        created_at_unix_ms=1_700_000_000_000,
        approved_at_unix_ms=1_700_000_001_000,
        feature_schema_version=feature_schema_version,
        protocol_version="calibration/2.0.0",
        camera=CalibrationCameraIdentity(
            identity_key=camera_key,
            device_name="FaceTime HD Camera",
            source="builtin",
            width=1280,
            height=720,
        ),
        metrics=(
            _metric(
                CalibrationMetricName.BLINK_RATE_PER_MIN,
                14.0,
                "blinks/min",
                CalibrationReferenceTask.REPRESENTATIVE_WORK,
                algorithms["blink"],
            ),
            _metric(
                CalibrationMetricName.NEUTRAL_HEAD_PITCH_DEG,
                2.0,
                "deg",
                CalibrationReferenceTask.NEUTRAL_HEAD_POSE,
                algorithms["head_pose"],
            ),
            _metric(
                CalibrationMetricName.NEUTRAL_FACE_SCALE_PX,
                180.0,
                "px",
                CalibrationReferenceTask.NEUTRAL_HEAD_POSE,
                algorithms["head_pose"],
            ),
        ),
        baselines=CalibrationBaselineValues(
            blink_rate_per_min=14.0,
            neutral_head_pitch_deg=2.0,
            neutral_face_scale_px=180.0,
        ),
    )


def _clock() -> FakeClock:
    return FakeClock(
        wall_unix_ms=1_700_000_010_000,
        mono_ns=9_000_000_000,
        _boot_id=_BOOT_ID,
    )


def _close(daemon: CortexDaemon) -> None:
    daemon._recorder.flush()  # noqa: SLF001 - isolated composition-root fixture


@pytest.mark.asyncio
async def test_direct_and_websocket_activation_emit_identical_domain_event(
    tmp_path: Path,
) -> None:
    """Both desktop transports terminate in the same application command."""

    direct_config = _config(tmp_path / "direct")
    websocket_config = _config(tmp_path / "websocket")
    direct = CortexDaemon(config=direct_config, clock=_clock())
    websocket = CortexDaemon(config=websocket_config, clock=_clock())
    profile = _profile(direct_config)
    for daemon in (direct, websocket):
        daemon._calibration_store.save_inactive(profile)  # noqa: SLF001

    try:
        with patch("cortex.services.runtime_daemon.uuid4", return_value=_EVENT_ID):
            # D9: the SQLite sync bridge refuses event-loop callers, so the
            # direct activation runs on a worker thread here exactly as the
            # desktop controller must dispatch it.
            direct_event = await asyncio.to_thread(
                direct.activate_calibration_profile,
                str(profile.profile_id),
                expected_sha256=calibration_profile_sha256(profile),
            )

        server = websocket._ws_server  # noqa: SLF001
        server.set_calibration_reload_callback(
            websocket.activate_calibration_profile
        )
        server.send_message = AsyncMock(return_value=1)  # type: ignore[method-assign]
        client = WebSocketClient(
            client_id="desktop-1",
            websocket=AsyncMock(),
            client_type="desktop",
            authenticated=True,
        )
        message = WSMessage(
            type=MessageType.CALIBRATION_RELOAD,
            payload={
                "profile_id": str(profile.profile_id),
                "profile_sha256": calibration_profile_sha256(profile),
            },
            correlation_id="calibration-cid",
        )
        with patch("cortex.services.runtime_daemon.uuid4", return_value=_EVENT_ID):
            await server._handle_calibration_reload(client, message)  # noqa: SLF001

        server.send_message.assert_awaited_once()
        message_type, websocket_payload = server.send_message.await_args.args
        assert message_type == MessageType.CALIBRATION_UPDATED.value
        assert websocket_payload == direct_event.model_dump(mode="json")
        assert websocket._active_calibration_profile == profile  # noqa: SLF001
        assert websocket._calibration_store.load_active() == profile  # noqa: SLF001
    finally:
        _close(direct)
        _close(websocket)
        registry.reset()


def test_rejected_candidate_preserves_pointer_and_every_live_reference(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    old = _profile(
        config,
        profile_id=UUID("00000000-0000-0000-0000-000000000410"),
    )
    store = CalibrationProfileStore(config.storage.path, clock=_clock())
    store.activate(old)
    daemon = CortexDaemon(config=config, clock=_clock())
    bad = _profile(
        config,
        profile_id=UUID("00000000-0000-0000-0000-000000000411"),
        feature_schema_version="features/999.0",
    )
    store.save_inactive(bad)
    references = (
        daemon._roi_extractor,  # noqa: SLF001
        daemon._pulse_estimator,  # noqa: SLF001
        daemon._physiology_v2,  # noqa: SLF001
        daemon._blink_detector,  # noqa: SLF001
        daemon._head_pose,  # noqa: SLF001
        daemon._posture,  # noqa: SLF001
        daemon._feature_fusion,  # noqa: SLF001
        daemon._scorer,  # noqa: SLF001
        daemon._smoother,  # noqa: SLF001
    )
    try:
        with pytest.raises(ValueError, match="feature schema"):
            daemon.activate_calibration_profile(
                str(bad.profile_id),
                expected_sha256=calibration_profile_sha256(bad),
            )
        assert store.load_active() == old
        assert daemon._active_calibration_profile == old  # noqa: SLF001
        assert references == (
            daemon._roi_extractor,  # noqa: SLF001
            daemon._pulse_estimator,  # noqa: SLF001
            daemon._physiology_v2,  # noqa: SLF001
            daemon._blink_detector,  # noqa: SLF001
            daemon._head_pose,  # noqa: SLF001
            daemon._posture,  # noqa: SLF001
            daemon._feature_fusion,  # noqa: SLF001
            daemon._scorer,  # noqa: SLF001
            daemon._smoother,  # noqa: SLF001
        )
    finally:
        _close(daemon)
        registry.reset()


def test_checksum_mismatch_cannot_change_active_profile(tmp_path: Path) -> None:
    config = _config(tmp_path)
    daemon = CortexDaemon(config=config, clock=_clock())
    profile = _profile(config)
    daemon._calibration_store.save_inactive(profile)  # noqa: SLF001
    try:
        with pytest.raises(ValueError, match="checksum mismatch"):
            daemon.activate_calibration_profile(
                str(profile.profile_id),
                expected_sha256="0" * 64,
            )
        assert daemon._calibration_store.load_active() is None  # noqa: SLF001
        assert daemon._active_calibration_profile is None  # noqa: SLF001
    finally:
        _close(daemon)
        registry.reset()


def test_algorithm_identity_mismatch_cannot_influence_runtime(tmp_path: Path) -> None:
    config = _config(tmp_path)
    daemon = CortexDaemon(config=config, clock=_clock())
    profile = _profile(config)
    first = profile.metrics[0]
    incompatible_algorithm = first.algorithm.model_copy(
        update={"configuration_sha256": "c" * 64}
    )
    incompatible = profile.model_copy(
        update={
            "metrics": (
                first.model_copy(update={"algorithm": incompatible_algorithm}),
                *profile.metrics[1:],
            )
        }
    )
    daemon._calibration_store.save_inactive(incompatible)  # noqa: SLF001
    try:
        with pytest.raises(ValueError, match="algorithm identity"):
            daemon.activate_calibration_profile(
                str(incompatible.profile_id),
                expected_sha256=calibration_profile_sha256(incompatible),
            )
        assert daemon._calibration_store.load_active() is None  # noqa: SLF001
        assert daemon._active_calibration_profile is None  # noqa: SLF001
    finally:
        _close(daemon)
        registry.reset()


def test_persistence_failure_cannot_partially_swap_live_graph(tmp_path: Path) -> None:
    config = _config(tmp_path)
    daemon = CortexDaemon(config=config, clock=_clock())
    profile = _profile(config)
    daemon._calibration_store.save_inactive(profile)  # noqa: SLF001
    references = (
        daemon._pulse_estimator,  # noqa: SLF001
        daemon._blink_detector,  # noqa: SLF001
        daemon._feature_fusion,  # noqa: SLF001
        daemon._scorer,  # noqa: SLF001
        daemon._smoother,  # noqa: SLF001
    )
    try:
        with (
            patch.object(
                daemon._calibration_store,  # noqa: SLF001
                "activate",
                side_effect=OSError("disk full"),
            ),
            pytest.raises(OSError, match="disk full"),
        ):
            daemon.activate_calibration_profile(
                str(profile.profile_id),
                expected_sha256=calibration_profile_sha256(profile),
            )
        assert daemon._active_calibration_profile is None  # noqa: SLF001
        assert references == (
            daemon._pulse_estimator,  # noqa: SLF001
            daemon._blink_detector,  # noqa: SLF001
            daemon._feature_fusion,  # noqa: SLF001
            daemon._scorer,  # noqa: SLF001
            daemon._smoother,  # noqa: SLF001
        )
    finally:
        _close(daemon)
        registry.reset()


def test_camera_mismatch_applies_non_camera_baselines_but_marks_proxy_unavailable(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    daemon = CortexDaemon(config=config, clock=_clock())
    profile = _profile(config, camera_key="builtin:other-camera")
    daemon._calibration_store.save_inactive(profile)  # noqa: SLF001
    daemon._active_camera_identity_key = "builtin:facetime-hd"  # noqa: SLF001
    try:
        event = daemon.activate_calibration_profile(
            str(profile.profile_id),
            expected_sha256=calibration_profile_sha256(profile),
        )
        assert event.camera_calibration_valid is False
        assert CalibrationMetricName.NEUTRAL_HEAD_PITCH_DEG.value not in event.applied_metrics
        assert CalibrationMetricName.NEUTRAL_FACE_SCALE_PX.value not in event.applied_metrics
        assert daemon._posture.is_calibrated is False  # noqa: SLF001
        assert daemon._blink_detector.baseline_blink_rate == 14.0  # noqa: SLF001
    finally:
        _close(daemon)
        registry.reset()


def test_startup_rejects_incompatible_active_profile_before_baseline_use(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    incompatible = _profile(config, feature_schema_version="features/999.0")
    CalibrationProfileStore(config.storage.path, clock=_clock()).activate(incompatible)

    daemon = CortexDaemon(config=config, clock=_clock())
    try:
        assert daemon._active_calibration_profile is None  # noqa: SLF001
        assert daemon._baseline_snapshot == UserBaselines()  # noqa: SLF001
        assert (  # noqa: SLF001
            daemon._blink_detector.baseline_blink_rate
            == UserBaselines().blink_rate_baseline
        )
    finally:
        _close(daemon)
        registry.reset()


def test_matching_camera_without_neutral_pose_evidence_is_not_calibrated(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    daemon = CortexDaemon(config=config, clock=_clock())
    complete = _profile(config)
    profile = complete.model_copy(
        update={
            "metrics": complete.metrics[:1],
            "baselines": CalibrationBaselineValues(blink_rate_per_min=14.0),
        }
    )
    daemon._calibration_store.save_inactive(profile)  # noqa: SLF001
    daemon._active_camera_identity_key = profile.camera.identity_key  # type: ignore[union-attr]  # noqa: SLF001
    daemon._active_camera_geometry = (1280, 720)  # noqa: SLF001
    try:
        event = daemon.activate_calibration_profile(
            str(profile.profile_id),
            expected_sha256=calibration_profile_sha256(profile),
        )
        assert event.camera_calibration_valid is False
        assert daemon._posture.is_calibrated is False  # noqa: SLF001
    finally:
        _close(daemon)
        registry.reset()


def test_camera_bound_proxy_is_ready_only_after_live_identity_match(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    daemon = CortexDaemon(config=config, clock=_clock())
    profile = _profile(config)
    daemon._calibration_store.save_inactive(profile)  # noqa: SLF001
    try:
        first = daemon.activate_calibration_profile(
            str(profile.profile_id),
            expected_sha256=calibration_profile_sha256(profile),
        )
        assert first.camera_calibration_valid is False
        assert daemon._posture.is_calibrated is False  # noqa: SLF001

        daemon._active_camera_identity_key = profile.camera.identity_key  # type: ignore[union-attr]  # noqa: SLF001
        daemon._active_camera_geometry = (1280, 720)  # noqa: SLF001
        reloaded = daemon.reload_active_calibration(str(profile.profile_id))
        assert reloaded.camera_calibration_valid is True
        assert daemon._posture.is_calibrated is True  # noqa: SLF001
    finally:
        _close(daemon)
        registry.reset()


def test_camera_identity_and_geometry_transitions_invalidate_then_restore_proxy(
    tmp_path: Path,
) -> None:
    """Camera-bound neutral pose is never reused across physical geometry."""

    config = _config(tmp_path)
    daemon = CortexDaemon(config=config, clock=_clock())
    profile = _profile(config)
    daemon._calibration_store.save_inactive(profile)  # noqa: SLF001
    source_id = UUID("00000000-0000-0000-0000-000000000420")

    def identity(key: str, width: int, height: int) -> CameraIdentity:
        return CameraIdentity(
            identity_key=key,
            device_id=0,
            device_name="FaceTime HD Camera",
            source="builtin",
            width=width,
            height=height,
        )

    def observation(sequence: int, camera: CameraIdentity) -> ObservationEnvelope[dict[str, str]]:
        return ObservationEnvelope[dict[str, str]](
            source=ObservationSource.CAMERA,
            source_instance_id=source_id,
            sequence=sequence,
            observed_at_unix_ms=1_700_000_010_000 + sequence,
            observed_at_mono_ns=9_000_000_000 + sequence,
            boot_id=_BOOT_ID,
            value={"identity": camera.identity_key},
            validity=ObservationValidity.VALID,
            quality=1.0,
            algorithm_version="test/1",
        )

    try:
        daemon.activate_calibration_profile(
            str(profile.profile_id),
            expected_sha256=calibration_profile_sha256(profile),
        )
        matching = identity(profile.camera.identity_key, 1280, 720)  # type: ignore[union-attr]
        resized = identity(profile.camera.identity_key, 640, 480)  # type: ignore[union-attr]

        daemon._handle_camera_identity(matching, observation(0, matching))  # noqa: SLF001
        assert daemon._camera_calibration_valid is True  # noqa: SLF001
        assert daemon._posture.is_calibrated is True  # noqa: SLF001

        daemon._handle_camera_identity(resized, observation(1, resized))  # noqa: SLF001
        assert daemon._camera_calibration_valid is False  # noqa: SLF001
        assert daemon._posture.is_calibrated is False  # noqa: SLF001

        daemon._handle_camera_identity(matching, observation(2, matching))  # noqa: SLF001
        assert daemon._camera_calibration_valid is True  # noqa: SLF001
        assert daemon._posture.is_calibrated is True  # noqa: SLF001
    finally:
        _close(daemon)
        registry.reset()


@pytest.mark.asyncio
async def test_non_desktop_client_is_rejected_without_invoking_callback() -> None:
    from cortex.libs.config.settings import APIConfig
    from cortex.services.api_gateway.websocket_server import WebSocketServer

    server = WebSocketServer(APIConfig(), clock=_clock())
    callback = AsyncMock()
    server.set_calibration_reload_callback(callback)
    client = WebSocketClient(
        client_id="browser-1",
        websocket=AsyncMock(),
        client_type="chrome",
        authenticated=True,
    )
    message = WSMessage(
        type=MessageType.CALIBRATION_RELOAD,
        payload={
            "profile_id": str(_PROFILE_ID),
            "profile_sha256": "a" * 64,
        },
        correlation_id="forbidden-cid",
    )
    await server._handle_calibration_reload(client, message)  # noqa: SLF001
    callback.assert_not_awaited()
    sent = WSMessage.from_json(client.websocket.send.await_args.args[0])
    assert sent.type == MessageType.CALIBRATION_UPDATE_FAILED.value
    assert sent.payload["code"] == "calibration_authority_required"
    assert sent.correlation_id == "forbidden-cid"
