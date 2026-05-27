"""
Cortex Feature Schemas

Pydantic models for frame metadata and feature vectors extracted from
webcam, face tracking, and telemetry sources.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class FrameMeta(BaseModel):
    """Metadata for a captured webcam frame."""

    timestamp: float = Field(
        ...,
        description=(
            "UNIX epoch seconds (wall-clock, UTC); comparable across "
            "producer and consumer. Previously documented as 'Monotonic' "
            "but the producer uses time.time(), not time.monotonic()."
        ),
    )
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
    # P1 Pipeline A: True when the per-frame quality gate rejected this
    # sample. Downstream consumers (rPPG window, kinematics) MUST treat
    # the frame as untrusted — append a NaN sentinel to keep window
    # length stable instead of polluting the signal with garbage.
    low_quality: bool = Field(
        False,
        description=(
            "Frame failed the per-frame quality gate. Consumers should "
            "skip the RGB sample or insert a NaN sentinel rather than "
            "appending the actual pixels."
        ),
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
    hrv_sdnn: float | None = Field(
        None, ge=0.0, description="SDNN of inter-beat intervals in ms"
    )
    hrv_pnn50: float | None = Field(
        None, ge=0.0, le=1.0, description="Fraction of adjacent IBI deltas > 50ms"
    )
    hrv_sd1: float | None = Field(
        None, ge=0.0, description="Poincare SD1 in ms"
    )
    hrv_sd2: float | None = Field(
        None, ge=0.0, description="Poincare SD2 in ms"
    )
    hrv_lf_hf_ratio: float | None = Field(
        None, ge=0.0, description="LF/HF ratio from Lomb-Scargle PSD"
    )
    hrv_sample_entropy: float | None = Field(
        None, ge=0.0, description="Sample entropy of IBI sequence"
    )
    physio_sqi: float | None = Field(
        None, ge=0.0, le=1.0, description="Composite physiological signal-quality index"
    )
    physio_sqi_components: dict[str, float] = Field(
        default_factory=dict,
        description="Named SQI components (nsqi, snr, motion, face_presence)",
    )
    hr_delta_5s: float | None = Field(
        None, description="Heart rate change over last 5 seconds (BPM/5s)"
    )
    respiration_rate_bpm: float | None = Field(
        None, ge=0.0, le=60.0, description="Respiration rate in breaths per minute"
    )
    valid: bool = Field(..., description="Whether physiological features are valid")

    @model_validator(mode="after")
    def _enforce_invalid_nulls(self) -> "PhysioFeatures":
        """P1-5: when ``valid`` is False, all data fields must be None.

        Signal-quality fields (``pulse_quality``, ``physio_sqi``,
        ``physio_sqi_components``) are exempt — they describe the signal
        quality itself, not the data. Every other numeric/optional field
        must be ``None`` when ``valid is False`` to prevent downstream
        consumers from silently using garbage values.
        """
        if not self.valid:
            _data_fields = (
                "pulse_bpm",
                "pulse_variability_proxy",
                "hrv_sdnn",
                "hrv_pnn50",
                "hrv_sd1",
                "hrv_sd2",
                "hrv_lf_hf_ratio",
                "hrv_sample_entropy",
                "hr_delta_5s",
                "respiration_rate_bpm",
            )
            bad = [f for f in _data_fields if getattr(self, f) is not None]
            if bad:
                raise ValueError(
                    f"PhysioFeatures(valid=False) must have None for data fields; "
                    f"got non-None values for: {bad}"
                )
        return self


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
    perclos_60s: float | None = Field(
        None, ge=0.0, le=1.0, description="PERCLOS over rolling 60 second window"
    )
    mean_blink_duration_ms: float | None = Field(
        None, ge=0.0, description="Mean blink duration in milliseconds"
    )
    ear_variance: float | None = Field(
        None, ge=0.0, description="Variance of eye aspect ratio over rolling window"
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
    correction_rate_per_100_keys: float | None = Field(
        None, ge=0.0, description="Backspace + undo corrections per 100 keypresses"
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
    scroll_back_rate_per_min: float | None = Field(
        None, ge=0.0, description="Upward reread scroll bursts per minute"
    )


class FeatureVector(BaseModel):
    """
    Unified 14-dimensional feature vector produced every 500ms.

    Combines physiological, kinematic, and telemetry features into a single
    vector for state classification.
    """

    timestamp: float = Field(
        ...,
        description=(
            "UNIX epoch seconds (wall-clock, UTC); comparable across "
            "producer and consumer. Previously documented as 'Monotonic' "
            "but the producer uses time.time(), not time.monotonic()."
        ),
    )

    # Physiological features (1-3)
    hr: float | None = Field(
        None, ge=30.0, le=220.0, description="Instantaneous heart rate (BPM)"
    )
    hrv_rmssd: float | None = Field(
        None, ge=0.0, description="HRV proxy - RMSSD in ms"
    )
    hrv_sdnn: float | None = Field(
        None, ge=0.0, description="SDNN in ms"
    )
    hr_delta: float | None = Field(
        None, description="Heart rate gradient over 5s"
    )
    physio_sqi: float | None = Field(
        None, ge=0.0, le=1.0, description="Composite physiological SQI"
    )

    # Kinematic features (4-7)
    blink_rate: float | None = Field(
        None, ge=0.0, le=60.0, description="Blinks per minute"
    )
    blink_rate_delta: float | None = Field(
        None, description="Blink rate change from baseline"
    )
    perclos_60s: float | None = Field(
        None, ge=0.0, le=1.0, description="PERCLOS in rolling 60s window"
    )
    ear_variance: float | None = Field(
        None, ge=0.0, description="EAR variance over rolling window"
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
    correction_rate_per_100_keys: float | None = Field(
        None, ge=0.0, description="Backspace + undo corrections per 100 keys"
    )
    tab_switch_frequency: float = Field(
        0.0, ge=0.0, description="Tab/window switches per minute"
    )
    scroll_back_rate_per_min: float | None = Field(
        None, ge=0.0, description="Upward reread scroll bursts per minute"
    )
    respiration_rate: float | None = Field(
        None, ge=0.0, le=60.0, description="Respiration rate (breaths/min)"
    )
    thrashing_score: float = Field(
        0.0, ge=0.0, le=1.0, description="Focus thrashing score from transition graph"
    )
    physio_missing: bool = Field(
        False,
        description=(
            "True when the physiological channel was unavailable or invalid "
            "at fuse-time. Downstream gates (HYPER scoring) must defer "
            "triggering when this flag is True."
        ),
    )
    telemetry_seen_count: int = Field(
        0,
        ge=0,
        description=(
            "Number of telemetry samples seen so far in this session. "
            "HYPO scoring contributions from telemetry only count after "
            "5+ samples (warm-up gate)."
        ),
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
