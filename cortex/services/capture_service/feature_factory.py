"""Single production factory for camera-derived feature components.

Runtime and calibration must use the same algorithms and configuration.
Keeping construction here prevents calibration from quietly reintroducing a
legacy estimator, a different threshold, or a frame-rate assumption.
"""

from __future__ import annotations

from dataclasses import dataclass

from cortex.libs.config.settings import CortexConfig
from cortex.libs.schemas.physiology import SignalAlgorithmIdentity
from cortex.services.capture_service.algorithm_identity import (
    calibration_algorithm_identity,
)
from cortex.services.kinematics_engine.blink_detector import BlinkDetector
from cortex.services.kinematics_engine.head_pose import HeadPoseEstimator
from cortex.services.kinematics_engine.posture import PostureAnalyzer
from cortex.services.physio_engine.roi_extractor import RoiExtractor
from cortex.services.physio_engine.v2.engine import PhysiologyEngineV2
from cortex.services.telemetry_engine.feature_aggregator import FeatureAggregator


@dataclass(frozen=True)
class ProductionCameraFeatureComponents:
    """Fresh stateful components for one capture ownership interval."""

    roi_extractor: RoiExtractor
    physiology: PhysiologyEngineV2
    blink: BlinkDetector
    head_pose: HeadPoseEstimator
    head_neck_proxy: PostureAnalyzer


def build_production_camera_feature_components(
    config: CortexConfig,
) -> ProductionCameraFeatureComponents:
    """Construct the exact estimator set used by runtime and calibration."""

    return ProductionCameraFeatureComponents(
        roi_extractor=RoiExtractor(config.landmarks),
        physiology=PhysiologyEngineV2(config.signal.rppg),
        blink=BlinkDetector(
            blink_config=config.signal.blink,
            landmarks_config=config.landmarks,
        ),
        head_pose=HeadPoseEstimator(
            frame_width=config.capture.width,
            frame_height=config.capture.height,
        ),
        head_neck_proxy=PostureAnalyzer(config.signal.posture),
    )


def production_calibration_algorithm_identities(
    config: CortexConfig,
    *,
    components: ProductionCameraFeatureComponents | None = None,
) -> dict[str, SignalAlgorithmIdentity]:
    """Return identities that a profile must match before influencing runtime."""

    current = components or build_production_camera_feature_components(config)
    return {
        "blink": calibration_algorithm_identity(
            current.blink,
            name="elapsed-time-ear-blink",
            version="2.0.0",
            parameters={
                "ear_threshold": config.signal.blink.ear_threshold,
                "min_closed_ms": config.signal.blink.min_closed_ms,
                "history_window_seconds": config.signal.blink.history_window_seconds,
            },
        ),
        "head_pose": calibration_algorithm_identity(
            current.head_pose,
            name="solvepnp-head-pose",
            version="2.0.0",
            parameters={
                "width": config.capture.width,
                "height": config.capture.height,
            },
        ),
        "telemetry": calibration_algorithm_identity(
            FeatureAggregator,
            name="representative-work-input-aggregation",
            version="2.0.0",
            parameters={"window_seconds": config.telemetry.window_seconds},
        ),
    }
