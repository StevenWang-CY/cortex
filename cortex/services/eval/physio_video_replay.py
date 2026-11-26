"""Replay recorded face video through the production camera path.

Reference datasets ship video plus a contact sensor.  To score what the app
would have measured, each frame goes through the same face tracker, frame
quality gate and skin-region extraction the runtime daemon uses; the
resulting per-frame trace is then replayed through the production physiology
processor.  Splitting the two steps lets a trace be extracted once (the slow
part) and re-scored whenever an estimator changes.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from uuid import uuid4

import numpy as np
from numpy.typing import NDArray

from cortex.libs.config.settings import CortexConfig
from cortex.libs.schemas.observations import CameraIdentity, ObservationValidity
from cortex.services.capture_service.pipeline import CapturePipeline
from cortex.services.capture_service.webcam import CapturedFrame
from cortex.services.eval.physio_qualification import StrideRecord, replay_trace
from cortex.services.physio_engine.camera_physiology import (
    numeric_observation_from_capture,
)
from cortex.services.physio_engine.roi_extractor import RoiExtractor


@dataclass(frozen=True)
class CameraTrace:
    """Per-frame output of the capture path for one recording.

    Rows of ``rgb`` are NaN where the capture path produced no usable skin
    sample (no face, failed quality gate); ``valid`` marks the usable rows.
    """

    sample_times_s: NDArray[np.float64]
    rgb: NDArray[np.float64]
    valid: NDArray[np.bool_]
    quality: NDArray[np.float64]
    motion_face_widths_per_second: NDArray[np.float64]
    head_vertical_face_units: NDArray[np.float64]

    @property
    def valid_fraction(self) -> float:
        return float(np.mean(self.valid)) if len(self.valid) else 0.0

    def save(self, path: str) -> None:
        np.savez_compressed(
            path,
            sample_times_s=self.sample_times_s,
            rgb=self.rgb,
            valid=self.valid,
            quality=self.quality,
            motion=self.motion_face_widths_per_second,
            head=self.head_vertical_face_units,
        )

    @classmethod
    def load(cls, path: str) -> CameraTrace:
        with np.load(path, allow_pickle=False) as data:
            return cls(
                sample_times_s=np.asarray(data["sample_times_s"], dtype=np.float64),
                rgb=np.asarray(data["rgb"], dtype=np.float64),
                valid=np.asarray(data["valid"], dtype=np.bool_),
                quality=np.asarray(data["quality"], dtype=np.float64),
                motion_face_widths_per_second=np.asarray(
                    data["motion"], dtype=np.float64
                ),
                head_vertical_face_units=np.asarray(data["head"], dtype=np.float64),
            )


def extract_camera_trace(
    frames: Iterable[tuple[float, NDArray[np.uint8]]],
    *,
    config: CortexConfig | None = None,
) -> CameraTrace:
    """Run ``(capture_time_seconds, bgr_frame)`` pairs through the capture path.

    Capture times must be strictly increasing; they stand in for the camera's
    monotonic clock, so a dataset's real frame timestamps should be used when
    it provides them.
    """

    cfg = config or CortexConfig()
    roi_extractor = RoiExtractor(
        cfg.landmarks, skin_region=cfg.signal.rppg.skin_region
    )
    pipeline = CapturePipeline(cfg.capture)
    boot = uuid4()
    source = uuid4()
    origin_ns = 1_000_000_000_000
    times: list[float] = []
    rgb: list[NDArray[np.float64]] = []
    valid: list[bool] = []
    quality: list[float] = []
    motion: list[float] = []
    head: list[float] = []
    identity: CameraIdentity | None = None
    missing = np.full(3, np.nan, dtype=np.float64)
    try:
        for index, (stamp_s, frame) in enumerate(frames):
            mono_ns = origin_ns + int(round(float(stamp_s) * 1e9))
            if identity is None:
                identity = CameraIdentity(
                    identity_key="dataset-replay",
                    device_id=0,
                    device_name="dataset replay",
                    source="replay",
                    backend=None,
                    width=int(frame.shape[1]),
                    height=int(frame.shape[0]),
                )
            output = pipeline.process_offline_frame(
                CapturedFrame(
                    frame=frame,
                    timestamp=float(stamp_s),
                    sequence=index,
                    observed_at_unix_ms=mono_ns // 1_000_000,
                    observed_at_mono_ns=mono_ns,
                    boot_id=boot,
                    source_instance_id=source,
                    camera_identity=identity,
                )
            )
            numeric = numeric_observation_from_capture(
                observation=output.observation,
                frame=output.frame,
                landmarks_px=output.landmarks_px,
                roi_extractor=roi_extractor,
            )
            usable = (
                numeric.validity == ObservationValidity.VALID.value
                and numeric.value is not None
            )
            times.append(float(stamp_s))
            valid.append(bool(usable))
            rgb.append(
                np.asarray(numeric.value, dtype=np.float64)
                if usable and numeric.value is not None
                else missing
            )
            quality.append(float(numeric.quality))
            motion.append(
                float(numeric.motion_face_widths_per_second)
                if numeric.motion_face_widths_per_second is not None
                else float("nan")
            )
            head.append(
                float(numeric.head_vertical_face_units)
                if numeric.head_vertical_face_units is not None
                else float("nan")
            )
    finally:
        pipeline.release_offline()
    return CameraTrace(
        sample_times_s=np.asarray(times, dtype=np.float64),
        rgb=np.asarray(rgb, dtype=np.float64).reshape(-1, 3),
        valid=np.asarray(valid, dtype=np.bool_),
        quality=np.asarray(quality, dtype=np.float64),
        motion_face_widths_per_second=np.asarray(motion, dtype=np.float64),
        head_vertical_face_units=np.asarray(head, dtype=np.float64),
    )


def replay_camera_trace(
    trace: CameraTrace,
    *,
    config: CortexConfig | None = None,
) -> list[StrideRecord]:
    """Replay an extracted trace through the production physiology processor."""

    return replay_trace(
        trace.sample_times_s,
        np.where(trace.valid[:, None], trace.rgb, 0.0),
        trace.head_vertical_face_units,
        config=config,
        observation_quality=trace.quality,
        valid_mask=trace.valid,
        motion_face_widths_per_second=trace.motion_face_widths_per_second,
    )


def replay_video(
    frames: Iterable[tuple[float, NDArray[np.uint8]]],
    *,
    config: CortexConfig | None = None,
) -> tuple[CameraTrace, list[StrideRecord]]:
    """Extract a trace from video frames and replay it in one call."""

    trace = extract_camera_trace(frames, config=config)
    return trace, replay_camera_trace(trace, config=config)
