"""
State Engine — Feature Fusion

Consumes PhysioFeatures, KinematicFeatures, and TelemetryFeatures,
producing a unified 12-dimensional FeatureVector every 500ms.

Handles missing channels (None values) with confidence weighting and
tracks per-channel signal quality for downstream state classification.
"""

from __future__ import annotations

import logging
import time

from cortex.libs.schemas.features import (
    FeatureVector,
    KinematicFeatures,
    PhysioFeatures,
    TelemetryFeatures,
)
from cortex.libs.schemas.state import SignalQuality

logger = logging.getLogger(__name__)


class FeatureFusion:
    """
    Fuses multi-channel features into a unified FeatureVector.

    Accepts PhysioFeatures, KinematicFeatures, and TelemetryFeatures
    independently (any may be None if the channel is unavailable),
    and produces a unified 12-dimensional FeatureVector with associated
    signal quality metrics.

    Usage:
        fusion = FeatureFusion()
        fusion.update_physio(physio_features)
        fusion.update_kinematics(kinematic_features)
        fusion.update_telemetry(telemetry_features)
        vector, quality = fusion.fuse()
    """

    def __init__(self) -> None:
        self._physio: PhysioFeatures | None = None
        self._kinematics: KinematicFeatures | None = None
        self._telemetry: TelemetryFeatures | None = None

        self._physio_timestamp: float = 0.0
        self._kinematics_timestamp: float = 0.0
        self._telemetry_timestamp: float = 0.0

    def update_physio(
        self, features: PhysioFeatures, timestamp: float | None = None,
    ) -> None:
        """Update the physiological feature channel."""
        self._physio = features
        self._physio_timestamp = timestamp or time.monotonic()

    def update_kinematics(
        self, features: KinematicFeatures, timestamp: float | None = None,
    ) -> None:
        """Update the kinematic feature channel."""
        self._kinematics = features
        self._kinematics_timestamp = timestamp or time.monotonic()

    def update_telemetry(
        self, features: TelemetryFeatures, timestamp: float | None = None,
    ) -> None:
        """Update the telemetry feature channel."""
        self._telemetry = features
        self._telemetry_timestamp = timestamp or time.monotonic()

    def fuse(self, timestamp: float | None = None) -> tuple[FeatureVector, SignalQuality]:
        """
        Produce a unified FeatureVector from all available channels.

        Missing channels contribute None values to the vector and
        reduce the corresponding signal quality score.

        Args:
            timestamp: Override timestamp. Defaults to now.

        Returns:
            (FeatureVector, SignalQuality) tuple.
        """
        now = timestamp or time.monotonic()

        # Physio features (dimensions 1-3 + respiration)
        hr = None
        hrv_rmssd = None
        hr_delta = None
        respiration_rate = None
        if self._physio is not None and self._physio.valid:
            hr = self._physio.pulse_bpm
            hrv_rmssd = self._physio.pulse_variability_proxy
            hr_delta = self._physio.hr_delta_5s
            respiration_rate = self._physio.respiration_rate_bpm

        # Kinematic features (dimensions 4-7)
        blink_rate = None
        blink_rate_delta = None
        shoulder_drop_ratio = None
        forward_lean_angle = None
        if self._kinematics is not None:
            blink_rate = self._kinematics.blink_rate
            blink_rate_delta = self._kinematics.blink_rate_delta
            shoulder_drop_ratio = self._kinematics.shoulder_drop_ratio
            if self._kinematics.forward_lean_score is not None:
                # Convert 0-1 score to 0-45 degree range for FeatureVector
                forward_lean_angle = self._kinematics.forward_lean_score * 45.0

        # Telemetry features (dimensions 8-12)
        mouse_velocity_mean = 0.0
        mouse_velocity_variance = 0.0
        click_frequency = 0.0
        keystroke_interval_variance = 0.0
        tab_switch_frequency = 0.0
        if self._telemetry is not None:
            mouse_velocity_mean = self._telemetry.mouse_velocity_mean
            mouse_velocity_variance = self._telemetry.mouse_velocity_variance
            click_frequency = self._telemetry.click_frequency
            keystroke_interval_variance = self._telemetry.keystroke_interval_variance
            tab_switch_frequency = self._telemetry.window_switch_rate

        vector = FeatureVector(
            timestamp=now,
            hr=hr,
            hrv_rmssd=hrv_rmssd,
            hr_delta=hr_delta,
            blink_rate=blink_rate,
            blink_rate_delta=blink_rate_delta,
            shoulder_drop_ratio=shoulder_drop_ratio,
            forward_lean_angle=forward_lean_angle,
            mouse_velocity_mean=mouse_velocity_mean,
            mouse_velocity_variance=mouse_velocity_variance,
            click_frequency=click_frequency,
            keystroke_interval_variance=keystroke_interval_variance,
            tab_switch_frequency=tab_switch_frequency,
            respiration_rate=respiration_rate,
        )

        quality = self._compute_signal_quality(now)

        return vector, quality

    def _compute_signal_quality(self, now: float) -> SignalQuality:
        """
        Compute per-channel signal quality.

        Quality is based on:
        1. Whether the channel is available
        2. Data freshness (staleness penalty)
        3. Channel-specific validity
        """
        # Staleness threshold: data older than 3s is considered stale
        stale_threshold = 3.0

        # Physio quality
        physio_q = 0.0
        if self._physio is not None and self._physio.valid:
            physio_q = self._physio.pulse_quality
            staleness = now - self._physio_timestamp
            if staleness > stale_threshold:
                physio_q *= max(0.0, 1.0 - (staleness - stale_threshold) / 10.0)

        # Kinematics quality
        kinematics_q = 0.0
        if self._kinematics is not None:
            kinematics_q = self._kinematics.confidence
            staleness = now - self._kinematics_timestamp
            if staleness > stale_threshold:
                kinematics_q *= max(0.0, 1.0 - (staleness - stale_threshold) / 10.0)

        # Telemetry quality (always available if present; check freshness)
        telemetry_q = 0.0
        if self._telemetry is not None:
            # Telemetry is high quality if we have recent data
            telemetry_q = 1.0
            staleness = now - self._telemetry_timestamp
            if staleness > stale_threshold:
                telemetry_q *= max(0.0, 1.0 - (staleness - stale_threshold) / 10.0)
            # Reduce quality if no input activity
            if self._telemetry.inactivity_seconds > 30.0:
                telemetry_q *= 0.5

        return SignalQuality(
            physio=min(1.0, max(0.0, physio_q)),
            kinematics=min(1.0, max(0.0, kinematics_q)),
            telemetry=min(1.0, max(0.0, telemetry_q)),
        )

    def reset(self) -> None:
        """Clear all channel data."""
        self._physio = None
        self._kinematics = None
        self._telemetry = None
        self._physio_timestamp = 0.0
        self._kinematics_timestamp = 0.0
        self._telemetry_timestamp = 0.0
