"""
Cortex Feature Schemas

Pydantic models for frame metadata and feature vectors extracted from
webcam, face tracking, and telemetry sources.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cortex.libs.schemas.observations import MissingReason
from cortex.libs.schemas.physiology import SignalEstimate


class FeatureName(StrEnum):
    """Stable names for measurements that may reach support inference.

    The names describe measurements, not interpretations. Whether a feature
    is eligible for a production rule or a research model is owned by the
    versioned catalog in ``state_engine.feature_schema``.
    """

    HEART_RATE_BPM = "heart_rate_bpm"
    BLINK_RATE_PER_MIN = "blink_rate_per_min"
    HEAD_NECK_FLEXION_SCORE = "head_neck_flexion_score"
    MOUSE_VELOCITY_MEAN = "mouse_velocity_mean"
    MOUSE_VELOCITY_VARIANCE = "mouse_velocity_variance"
    CLICK_FREQUENCY = "click_frequency"
    KEYPRESS_RATE_PER_MIN = "keypress_rate_per_min"
    KEYSTROKE_INTERVAL_VARIANCE = "keystroke_interval_variance"
    CORRECTION_RATE_PER_100_KEYS = "correction_rate_per_100_keys"
    INACTIVITY_SECONDS = "inactivity_seconds"
    TAB_SWITCH_RATE_PER_MIN = "tab_switch_rate_per_min"
    SCROLL_BACK_RATE_PER_MIN = "scroll_back_rate_per_min"
    THRASHING_SCORE = "thrashing_score"


class FeatureValue(BaseModel):
    """One named, provenance-bearing input to the support engine."""

    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    value: float | None = None
    valid: bool = False
    quality: float = Field(0.0, ge=0.0, le=1.0)
    age_ms: int = Field(0, ge=0)
    source_window_ms: int = Field(..., ge=0)
    algorithm_version: str = Field(..., min_length=1)
    missing_reason: MissingReason | None = None

    @model_validator(mode="after")
    def _validity_is_coherent(self) -> FeatureValue:
        if self.valid:
            if self.value is None:
                raise ValueError("valid feature values must contain a value")
            if self.missing_reason is not None:
                raise ValueError("valid feature values cannot have a missing_reason")
        else:
            if self.value is not None:
                raise ValueError("invalid feature values cannot contain a value")
            if self.missing_reason is None:
                raise ValueError("invalid feature values require a missing_reason")
        return self


class FrameMeta(BaseModel):
    """Metadata for a captured webcam frame."""

    timestamp: float = Field(
        ...,
        deprecated=True,
        description=(
            "UNIX epoch seconds (wall-clock, UTC); comparable across "
            "producer and consumer. Previously documented as 'Monotonic' "
            "but the producer uses time.time(), not time.monotonic()."
        ),
    )
    schema_version: Literal["2.0"] = "2.0"
    observed_at_unix_ms: int | None = Field(
        None,
        ge=0,
        description="Capture time in UTC Unix epoch milliseconds.",
    )
    observed_at_mono_ns: int | None = Field(
        None,
        ge=0,
        description="Capture time in the producer's monotonic clock domain.",
    )
    boot_id: UUID | None = Field(
        None,
        description="Producer boot ID; required when observed_at_mono_ns is present.",
    )
    frame_available: bool = Field(
        True,
        description=(
            "Whether this scheduled observation contains pixels. False means "
            "the timestamp represents a missing camera read, not a live frame."
        ),
    )
    missing_reason: MissingReason | None = Field(
        None,
        description="Reason pixels were unavailable for this scheduled observation.",
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

    @model_validator(mode="after")
    def _validate_v2_time_tuple(self) -> FrameMeta:
        supplied = (
            self.observed_at_unix_ms is not None,
            self.observed_at_mono_ns is not None,
            self.boot_id is not None,
        )
        if any(supplied) and not all(supplied):
            raise ValueError(
                "observed_at_unix_ms, observed_at_mono_ns, and boot_id "
                "must be supplied together"
            )
        if self.frame_available:
            if self.missing_reason is not None:
                raise ValueError(
                    "available frames cannot carry a missing_reason"
                )
        else:
            if self.missing_reason is None:
                raise ValueError(
                    "missing frames require a missing_reason"
                )
            if self.face_detected:
                raise ValueError(
                    "missing frames cannot report a detected face"
                )
        return self


class PhysioFeatures(BaseModel):
    """Physiological features extracted from rPPG analysis."""

    pulse_bpm: float | None = Field(
        None, ge=30.0, le=220.0, description="Instantaneous heart rate in BPM"
    )
    pulse_quality: float = Field(
        ..., ge=0.0, le=1.0, description="Signal quality (SNR-based, 0-1)"
    )
    pulse_variability_proxy: float | None = Field(
        None,
        ge=0.0,
        description="Compatibility field; unavailable in product pending validation",
    )
    hrv_sdnn: float | None = Field(
        None, ge=0.0, description="Compatibility field; unavailable in product"
    )
    hrv_pnn50: float | None = Field(
        None, ge=0.0, le=1.0, description="Compatibility field; unavailable in product"
    )
    hrv_sd1: float | None = Field(
        None, ge=0.0, description="Compatibility field; unavailable in product"
    )
    hrv_sd2: float | None = Field(
        None, ge=0.0, description="Compatibility field; unavailable in product"
    )
    hrv_lf_hf_ratio: float | None = Field(
        None, ge=0.0, description="Compatibility field; unavailable in product"
    )
    hrv_sample_entropy: float | None = Field(
        None, ge=0.0, description="Compatibility field; unavailable in product"
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
        None,
        ge=0.0,
        le=60.0,
        description="Compatibility field; unavailable in product pending validation",
    )
    pulse_evidence: SignalEstimate | None = Field(
        None,
        description="Algorithm/version/quality/uncertainty contract for pulse",
    )
    hrv_evidence: dict[str, SignalEstimate] = Field(
        default_factory=dict,
        description="Metric-specific HRV readiness; unavailable metrics stay explicit",
    )
    respiration_evidence: SignalEstimate | None = Field(
        None,
        description="Agreement-gated breathing-rate proxy evidence contract",
    )
    valid: bool = Field(..., description="Whether physiological features are valid")

    @model_validator(mode="after")
    def _enforce_invalid_nulls(self) -> PhysioFeatures:
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
    blink_valid_exposure_seconds: float = Field(
        0.0,
        ge=0.0,
        description="Eye-visible monotonic exposure contributing to blink metrics",
    )
    head_pitch: float | None = Field(None, description="Head pitch angle in degrees")
    head_yaw: float | None = Field(None, description="Head yaw angle in degrees")
    head_roll: float | None = Field(None, description="Head roll angle in degrees")
    head_angular_velocity_deg_per_s: float | None = Field(
        None,
        ge=0.0,
        description="Elapsed-time head angular velocity in degrees per second",
    )
    head_is_jittery: bool | None = None
    head_is_frozen: bool | None = None
    head_neck_flexion_angle: float | None = Field(
        None,
        ge=0.0,
        le=90.0,
        description="Camera-relative head/neck flexion proxy in degrees",
    )
    head_neck_flexion_score: float | None = Field(
        None,
        ge=0.0,
        le=1.0,
        description="Calibrated head/neck flexion proxy score",
    )
    head_neck_flexion_dwell_seconds: float = Field(
        0.0,
        ge=0.0,
        description="Contiguous valid time above the flexion threshold",
    )
    head_neck_proxy_available: bool = False
    slump_score: float | None = Field(
        None,
        ge=0.0,
        le=1.0,
        description="Deprecated compatibility field; no body-posture model runs",
    )
    forward_lean_score: float | None = Field(
        None,
        ge=0.0,
        le=1.0,
        description="Deprecated compatibility alias for head/neck flexion",
    )
    shoulder_drop_ratio: float | None = Field(
        None,
        ge=0.0,
        le=1.0,
        description="Unavailable compatibility field; shoulders are not measured",
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
    keypress_rate_per_min: float | None = Field(
        None,
        ge=0.0,
        description="Observed typing-key presses per minute over the source window",
    )
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
    observation_window_seconds: float | None = Field(
        None,
        gt=0.0,
        description="Actual telemetry aggregation window; required by v2 inference",
    )
    mouse_move_count: int | None = Field(None, ge=0)
    click_press_count: int | None = Field(None, ge=0)
    key_press_count: int | None = Field(None, ge=0)
    scroll_event_count: int | None = Field(None, ge=0)
    window_focus_event_count: int | None = Field(None, ge=0)
    window_focus_source_available: bool = Field(
        False,
        description=(
            "Whether window tracking was running or produced an observed event; "
            "false means zero switch rate is not an observation."
        ),
    )


class FeatureVector(BaseModel):
    """
    Unified feature snapshot produced every 500ms.

    ``features`` is the canonical v2 contract. The flat fields remain for one
    compatibility cycle while non-inference consumers migrate. A missing
    source is represented by an invalid :class:`FeatureValue`, never by a
    fabricated numeric zero.
    """

    timestamp: float = Field(
        ...,
        deprecated=True,
        description=(
            "Deprecated v1 state-pipeline monotonic seconds. Compare only "
            "inside the producing process; prefer observed_at_mono_ns."
        ),
    )
    schema_version: Literal["2.0"] = "2.0"
    observed_at_unix_ms: int | None = Field(
        None,
        ge=0,
        description="UTC Unix epoch milliseconds for persistence/display.",
    )
    observed_at_mono_ns: int | None = Field(
        None,
        ge=0,
        description="Monotonic nanoseconds for elapsed-time decisions.",
    )
    boot_id: UUID | None = Field(
        None,
        description="Clock domain for observed_at_mono_ns.",
    )
    features: dict[FeatureName, FeatureValue] = Field(
        default_factory=dict,
        description="Named measurements with validity, quality, age, and provenance.",
    )

    # Physiological features (1-3)
    hr: float | None = Field(
        None, ge=30.0, le=220.0, description="Instantaneous heart rate (BPM)"
    )
    hrv_rmssd: float | None = Field(
        None, ge=0.0, description="Compatibility field; unavailable in product"
    )
    hrv_sdnn: float | None = Field(
        None, ge=0.0, description="Compatibility field; unavailable in product"
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
    blink_valid_exposure_seconds: float = Field(0.0, ge=0.0)
    head_angular_velocity_deg_per_s: float | None = Field(None, ge=0.0)
    head_is_jittery: bool | None = None
    head_is_frozen: bool | None = None
    head_neck_flexion_angle: float | None = Field(None, ge=0.0, le=90.0)
    head_neck_flexion_score: float | None = Field(None, ge=0.0, le=1.0)
    head_neck_flexion_dwell_seconds: float = Field(0.0, ge=0.0)
    head_neck_proxy_available: bool = False
    shoulder_drop_ratio: float | None = Field(
        None,
        ge=0.0,
        le=1.0,
        description="Unavailable compatibility field; shoulders are not measured",
    )
    forward_lean_angle: float | None = Field(
        None,
        ge=0.0,
        le=90.0,
        description="Deprecated alias for camera-relative head/neck flexion",
    )

    # Telemetry features (8-12)
    mouse_velocity_mean: float = Field(
        0.0, ge=0.0, description="Mean mouse velocity (px/s)"
    )
    mouse_velocity_variance: float = Field(
        0.0, ge=0.0, description="Mouse velocity variance"
    )
    click_frequency: float = Field(0.0, ge=0.0, description="Clicks per second")
    keypress_rate_per_min: float = Field(
        0.0, ge=0.0, description="Typing-key presses per minute"
    )
    keystroke_interval_variance: float = Field(
        0.0, ge=0.0, description="Keystroke interval variance (ms^2)"
    )
    correction_rate_per_100_keys: float | None = Field(
        None, ge=0.0, description="Backspace + undo corrections per 100 keys"
    )
    inactivity_seconds: float = Field(
        0.0,
        ge=0.0,
        description=(
            "Seconds since the last input event. The value is evidence only "
            "when the named telemetry feature is valid."
        ),
    )
    tab_switch_frequency: float = Field(
        0.0, ge=0.0, description="Tab/window switches per minute"
    )
    scroll_back_rate_per_min: float | None = Field(
        None, ge=0.0, description="Upward reread scroll bursts per minute"
    )
    respiration_rate: float | None = Field(
        None,
        ge=0.0,
        le=60.0,
        description="Compatibility field; unavailable in product",
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

    @model_validator(mode="after")
    def _validate_v2_time_tuple(self) -> FeatureVector:
        supplied = (
            self.observed_at_unix_ms is not None,
            self.observed_at_mono_ns is not None,
            self.boot_id is not None,
        )
        if any(supplied) and not all(supplied):
            raise ValueError(
                "observed_at_unix_ms, observed_at_mono_ns, and boot_id "
                "must be supplied together"
            )
        return self

    def to_array(self) -> list[float | None]:
        """Legacy array projection; production inference does not use it.

        New model code must use ``FeatureSchema.to_ordered_array`` so input
        order, transformations, missingness, and exact dimension are versioned.
        """
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
        return self.blink_rate is not None or self.head_neck_proxy_available

    @property
    def has_telemetry(self) -> bool:
        """Whether a real telemetry snapshot has been observed.

        Zero activity is a legitimate observation, so value-based truthiness
        would incorrectly call an idle but connected input stream missing.
        """
        return self.telemetry_seen_count > 0
