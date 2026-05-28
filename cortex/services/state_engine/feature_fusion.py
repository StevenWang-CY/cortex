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
        # Phase 4 fix #5/#7 invariant: the per-channel timestamp must always
        # be valid *by the time* the matching feature pointer becomes
        # non-None. ``update_*`` enforces this by assigning timestamp BEFORE
        # the feature pointer. The 0.0 sentinel below is safe because
        # ``_compute_signal_quality`` short-circuits on ``self._<chan> is
        # None`` before ever reading the timestamp, so the sentinel never
        # participates in a staleness computation.
        self._physio: PhysioFeatures | None = None
        self._kinematics: KinematicFeatures | None = None
        self._telemetry: TelemetryFeatures | None = None

        self._physio_timestamp: float = 0.0
        self._kinematics_timestamp: float = 0.0
        self._telemetry_timestamp: float = 0.0

        # P1 Pipeline A: cold-start counter so HYPO scoring can require
        # at least 5 telemetry samples before trusting "low activity"
        # signals (mouse drift, low tab-switch rate, etc.). Without the
        # warm-up gate the first 2-3 seconds after launch always looked
        # like HYPO disengagement.
        self._telemetry_seen_count: int = 0

    def update_physio(
        self, features: PhysioFeatures, timestamp: float | None = None,
    ) -> None:
        """Update the physiological feature channel.

        Phase 4 fix #5: timestamp is assigned BEFORE the feature pointer so
        any reader that observes ``self._physio is not None`` is guaranteed
        to see a matching ``_physio_timestamp``. asyncio is single-threaded
        today so this is belt-and-braces, but the ordering invariant must
        match the code regardless.
        """
        self._physio_timestamp = timestamp if timestamp is not None else time.monotonic()
        self._physio = features

    def update_kinematics(
        self, features: KinematicFeatures, timestamp: float | None = None,
    ) -> None:
        """Update the kinematic feature channel.

        Phase 4 fix #5: see ``update_physio`` for ordering rationale.
        """
        self._kinematics_timestamp = timestamp if timestamp is not None else time.monotonic()
        self._kinematics = features

    def update_telemetry(
        self, features: TelemetryFeatures, timestamp: float | None = None,
    ) -> None:
        """Update the telemetry feature channel.

        Phase 4 fix #5: see ``update_physio`` for ordering rationale.
        """
        self._telemetry_timestamp = timestamp if timestamp is not None else time.monotonic()
        self._telemetry = features
        self._telemetry_seen_count += 1

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
        hrv_sdnn = None
        hr_delta = None
        physio_sqi = None
        respiration_rate = None
        # P1 Pipeline A: explicit "physio missing" flag so downstream
        # gates can defer HYPER triggering when only kinematics/telemetry
        # are available. Setting None on the floats alone was not enough —
        # rule_scorer.score_hr_*/score_hrv_drop guarded on None but the
        # HYPER pathway as a whole had no way to know "physio is just
        # absent" vs "physio is present and normal".
        physio_missing = True
        if self._physio is not None and self._physio.valid:
            hr = self._physio.pulse_bpm
            hrv_rmssd = self._physio.pulse_variability_proxy
            hrv_sdnn = self._physio.hrv_sdnn
            hr_delta = self._physio.hr_delta_5s
            physio_sqi = self._physio.physio_sqi
            respiration_rate = self._physio.respiration_rate_bpm
            physio_missing = False

        # Kinematic features (dimensions 4-7)
        blink_rate = None
        blink_rate_delta = None
        perclos_60s = None
        ear_variance = None
        shoulder_drop_ratio = None
        forward_lean_angle = None
        if self._kinematics is not None:
            blink_rate = self._kinematics.blink_rate
            blink_rate_delta = self._kinematics.blink_rate_delta
            perclos_60s = self._kinematics.perclos_60s
            ear_variance = self._kinematics.ear_variance
            shoulder_drop_ratio = self._kinematics.shoulder_drop_ratio
            if self._kinematics.forward_lean_score is not None:
                # Convert 0-1 score to 0-45 degree range for FeatureVector.
                # P1 Pipeline A: when forward_lean_score is None we leave
                # forward_lean_angle as None instead of coercing to 0.0, so
                # downstream lean-AND-shoulder posture-HYPO scoring can
                # explicitly skip rather than score a false slump.
                forward_lean_angle = self._kinematics.forward_lean_score * 45.0

        # Telemetry features (dimensions 8-12)
        mouse_velocity_mean = 0.0
        mouse_velocity_variance = 0.0
        click_frequency = 0.0
        keystroke_interval_variance = 0.0
        correction_rate_per_100_keys = None
        tab_switch_frequency = 0.0
        scroll_back_rate_per_min = None
        if self._telemetry is not None:
            mouse_velocity_mean = self._telemetry.mouse_velocity_mean
            mouse_velocity_variance = self._telemetry.mouse_velocity_variance
            click_frequency = self._telemetry.click_frequency
            keystroke_interval_variance = self._telemetry.keystroke_interval_variance
            correction_rate_per_100_keys = self._telemetry.correction_rate_per_100_keys
            tab_switch_frequency = self._telemetry.window_switch_rate
            scroll_back_rate_per_min = self._telemetry.scroll_back_rate_per_min

        vector = FeatureVector(
            timestamp=now,
            hr=hr,
            hrv_rmssd=hrv_rmssd,
            hrv_sdnn=hrv_sdnn,
            hr_delta=hr_delta,
            physio_sqi=physio_sqi,
            blink_rate=blink_rate,
            blink_rate_delta=blink_rate_delta,
            perclos_60s=perclos_60s,
            ear_variance=ear_variance,
            shoulder_drop_ratio=shoulder_drop_ratio,
            forward_lean_angle=forward_lean_angle,
            mouse_velocity_mean=mouse_velocity_mean,
            mouse_velocity_variance=mouse_velocity_variance,
            click_frequency=click_frequency,
            keystroke_interval_variance=keystroke_interval_variance,
            correction_rate_per_100_keys=correction_rate_per_100_keys,
            tab_switch_frequency=tab_switch_frequency,
            scroll_back_rate_per_min=scroll_back_rate_per_min,
            respiration_rate=respiration_rate,
            physio_missing=physio_missing,
            telemetry_seen_count=self._telemetry_seen_count,
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
        self._telemetry_seen_count = 0
