"""
Cortex Feature Schemas

Pydantic models for frame metadata and feature vectors extracted from
webcam, face tracking, and telemetry sources.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class FrameMeta(BaseModel):
    """Metadata for a captured webcam frame."""

    timestamp: float = Field(..., description="Monotonic timestamp in seconds")
    face_detected: bool = Field(..., description="Whether a face was detected")
    face_confidence: float = Field(
        ..., ge=0.0, le=1.0, description="Face detection confidence score"
    )
    brightness_score: float = Field(
        ..., ge=0.0, le=1.0, description="Frame brightness quality score"
    )
    blur_score: float = Field(..., ge=0.0, le=1.0, description="Frame blur quality score")
    motion_score: float = Field(
        ..., ge=0.0, le=1.0, description="Inter-frame motion quality score"
    )


class PhysioFeatures(BaseModel):
    """Physiological features extracted from rPPG analysis."""

    pulse_bpm: float | None = Field(
        None, ge=30.0, le=220.0, description="Instantaneous heart rate in BPM"
    )
    pulse_quality: float = Field(
        ..., ge=0.0, le=1.0, description="Signal quality (SNR-based, 0-1)"
    )
    pulse_variability_proxy: float | None = Field(
        None, ge=0.0, description="RMSSD of inter-beat intervals in ms"
    )
    hr_delta_5s: float | None = Field(
        None, description="Heart rate change over last 5 seconds (BPM/5s)"
    )
    respiration_rate_bpm: float | None = Field(
        None, ge=0.0, le=60.0, description="Respiration rate in breaths per minute"
    )
    valid: bool = Field(..., description="Whether physiological features are valid")


class KinematicFeatures(BaseModel):
    """Kinematic features from face mesh and pose estimation."""

    blink_rate: float | None = Field(
        None, ge=0.0, le=60.0, description="Blinks per minute"
    )
    blink_rate_delta: float | None = Field(
        None, description="Change in blink rate from 60s baseline"
    )
    blink_suppression_score: float | None = Field(
        None, ge=0.0, le=1.0, description="Blink suppression indicator (0-1)"
    )
    head_pitch: float | None = Field(None, description="Head pitch angle in degrees")
    head_yaw: float | None = Field(None, description="Head yaw angle in degrees")
    head_roll: float | None = Field(None, description="Head roll angle in degrees")
    slump_score: float | None = Field(
        None, ge=0.0, le=1.0, description="Posture slump score (0-1)"
    )
    forward_lean_score: float | None = Field(
        None, ge=0.0, le=1.0, description="Forward lean indicator (0-1)"
    )
    shoulder_drop_ratio: float | None = Field(
        None, ge=0.0, le=1.0, description="Shoulder drop ratio from baseline"
    )
    confidence: float = Field(
        ..., ge=0.0, le=1.0, description="Overall kinematic feature confidence"
    )


class TelemetryFeatures(BaseModel):
    """Telemetry features from mouse, keyboard, and window tracking."""

    mouse_velocity_mean: float = Field(
        ..., ge=0.0, description="Mean mouse velocity in px/s"
    )
    mouse_velocity_variance: float = Field(
        ..., ge=0.0, description="Mouse velocity variance"
    )
    mouse_jerk_score: float = Field(
        ..., ge=0.0, le=1.0, description="Mouse jerk/erratic movement score"
    )
    click_burst_score: float = Field(
        ..., ge=0.0, le=1.0, description="Rapid clicking burst score"
    )
    click_frequency: float = Field(..., ge=0.0, description="Clicks per second")
    keyboard_burst_score: float = Field(
        ..., ge=0.0, le=1.0, description="Typing intensity burst score"
    )
    keystroke_interval_variance: float = Field(
        ..., ge=0.0, description="Variance in keystroke intervals (ms^2)"
    )
    backspace_density: float = Field(
        ..., ge=0.0, le=1.0, description="Ratio of backspaces to total keystrokes"
    )
    inactivity_seconds: float = Field(
        ..., ge=0.0, description="Seconds since last input event"
    )
    window_switch_rate: float = Field(
        ..., ge=0.0, description="Window/app switches per minute"
    )
    tab_count: int | None = Field(None, ge=0, description="Number of open browser tabs")
    scroll_reversal_score: float | None = Field(
        None, ge=0.0, le=1.0, description="Scroll direction reversal score"
    )


class FeatureVector(BaseModel):
    """
    Unified 14-dimensional feature vector produced every 500ms.

    Combines physiological, kinematic, and telemetry features into a single
    vector for state classification.
    """

    timestamp: float = Field(..., description="Monotonic timestamp in seconds")

    # Physiological features (1-3)
    hr: float | None = Field(
        None, ge=30.0, le=220.0, description="Instantaneous heart rate (BPM)"
    )
    hrv_rmssd: float | None = Field(
        None, ge=0.0, description="HRV proxy - RMSSD in ms"
    )
    hr_delta: float | None = Field(
        None, description="Heart rate gradient over 5s"
    )

    # Kinematic features (4-7)
    blink_rate: float | None = Field(
        None, ge=0.0, le=60.0, description="Blinks per minute"
    )
    blink_rate_delta: float | None = Field(
        None, description="Blink rate change from baseline"
    )
    shoulder_drop_ratio: float | None = Field(
        None, ge=0.0, le=1.0, description="Shoulder drop from baseline"
    )
    forward_lean_angle: float | None = Field(
        None, ge=0.0, le=90.0, description="Forward lean angle in degrees"
    )

    # Telemetry features (8-12)
    mouse_velocity_mean: float = Field(
        0.0, ge=0.0, description="Mean mouse velocity (px/s)"
    )
    mouse_velocity_variance: float = Field(
        0.0, ge=0.0, description="Mouse velocity variance"
    )
    click_frequency: float = Field(0.0, ge=0.0, description="Clicks per second")
    keystroke_interval_variance: float = Field(
        0.0, ge=0.0, description="Keystroke interval variance (ms^2)"
    )
    tab_switch_frequency: float = Field(
        0.0, ge=0.0, description="Tab/window switches per minute"
    )
    respiration_rate: float | None = Field(
        None, ge=0.0, le=60.0, description="Respiration rate (breaths/min)"
    )
    thrashing_score: float = Field(
        0.0, ge=0.0, le=1.0, description="Focus thrashing score from transition graph"
    )

    def to_array(self) -> list[float | None]:
        """Convert feature vector to a list for ML inference."""
        return [
            self.hr,
            self.hrv_rmssd,
            self.hr_delta,
            self.blink_rate,
            self.blink_rate_delta,
            self.shoulder_drop_ratio,
            self.forward_lean_angle,
            self.mouse_velocity_mean,
            self.mouse_velocity_variance,
            self.click_frequency,
            self.keystroke_interval_variance,
            self.tab_switch_frequency,
            self.respiration_rate,
            self.thrashing_score,
        ]

    @property
    def has_physio(self) -> bool:
        """Check if physiological features are available."""
        return self.hr is not None

    @property
    def has_respiration(self) -> bool:
        """Check if respiration features are available."""
        return self.respiration_rate is not None

    @property
    def has_kinematics(self) -> bool:
        """Check if kinematic features are available."""
        return self.blink_rate is not None or self.shoulder_drop_ratio is not None

    @property
    def has_telemetry(self) -> bool:
        """Check if telemetry features are non-zero."""
        return (
            self.mouse_velocity_mean > 0
            or self.click_frequency > 0
            or self.keystroke_interval_variance > 0
        )
