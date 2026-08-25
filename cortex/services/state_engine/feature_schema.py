"""Frozen feature catalog for deterministic and future model inference.

The catalog is the sole owner of feature order. It deliberately separates
measurement availability from production eligibility: camera-derived values
remain useful diagnostics, but are excluded from Level-A support rules until
the preregistered validation protocol has been completed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal

from cortex.libs.schemas.features import FeatureName, FeatureValue, FeatureVector

FeatureChannel = Literal["physio", "kinematics", "telemetry", "focus_graph"]
FeatureMaturity = Literal["production_rule", "diagnostic_only", "research_only"]


@dataclass(frozen=True, slots=True)
class FeatureDefinition:
    """Immutable metadata for one model-facing measurement."""

    name: FeatureName
    channel: FeatureChannel
    unit: str
    source_window_ms: int
    transform_version: str
    maturity: FeatureMaturity
    production_use: tuple[str, ...]
    exclusion_reason: str | None = None


FEATURE_SCHEMA_VERSION = "support-features-v2.1.0"

ORDERED_FEATURES: tuple[FeatureDefinition, ...] = (
    FeatureDefinition(
        FeatureName.HEART_RATE_BPM, "physio", "beats/min", 10_000,
        "identity-v1", "diagnostic_only", (),
        "Webcam pulse has not passed the reference-sensor product gate.",
    ),
    FeatureDefinition(
        FeatureName.BLINK_RATE_PER_MIN, "kinematics", "blinks/min", 60_000,
        "identity-v1", "diagnostic_only", (),
        "No participant-held-out evidence links this proxy to support need.",
    ),
    FeatureDefinition(
        FeatureName.HEAD_NECK_FLEXION_SCORE, "kinematics", "ratio", 5_000,
        "identity-v1", "diagnostic_only", (),
        "Camera-relative comfort proxy is not a cognitive-state feature.",
    ),
    FeatureDefinition(
        FeatureName.MOUSE_VELOCITY_MEAN, "telemetry", "px/s", 15_000,
        "identity-v1", "production_rule", ("flow_like", "under_engaged"),
    ),
    FeatureDefinition(
        FeatureName.MOUSE_VELOCITY_VARIANCE, "telemetry", "px2/s2", 15_000,
        "identity-v1", "production_rule", ("support_likely", "flow_like"),
    ),
    FeatureDefinition(
        FeatureName.CLICK_FREQUENCY, "telemetry", "clicks/s", 15_000,
        "identity-v1", "production_rule",
        ("support_likely", "flow_like", "under_engaged"),
    ),
    FeatureDefinition(
        FeatureName.KEYPRESS_RATE_PER_MIN, "telemetry", "keypresses/min", 15_000,
        "identity-v1", "production_rule", ("flow_like", "under_engaged"),
    ),
    FeatureDefinition(
        FeatureName.KEYSTROKE_INTERVAL_VARIANCE, "telemetry", "ms2", 15_000,
        "identity-v1", "production_rule", ("support_likely", "flow_like"),
    ),
    FeatureDefinition(
        FeatureName.CORRECTION_RATE_PER_100_KEYS, "telemetry", "count/100 keys",
        15_000, "identity-v1", "production_rule", ("support_likely", "flow_like"),
    ),
    FeatureDefinition(
        FeatureName.INACTIVITY_SECONDS, "telemetry", "seconds", 15_000,
        "identity-v1", "production_rule", ("under_engaged",),
    ),
    FeatureDefinition(
        FeatureName.TAB_SWITCH_RATE_PER_MIN, "telemetry", "switches/min", 60_000,
        "identity-v1", "production_rule",
        ("support_likely", "flow_like", "under_engaged"),
    ),
    FeatureDefinition(
        FeatureName.SCROLL_BACK_RATE_PER_MIN, "telemetry", "bursts/min", 60_000,
        "identity-v1", "production_rule", ("support_likely", "flow_like"),
    ),
    FeatureDefinition(
        FeatureName.THRASHING_SCORE, "focus_graph", "ratio", 60_000,
        "identity-v1", "production_rule", ("support_likely", "flow_like"),
    ),
)

if len({item.name for item in ORDERED_FEATURES}) != len(ORDERED_FEATURES):
    raise RuntimeError("feature catalog contains duplicate names")
if {item.name for item in ORDERED_FEATURES} != set(FeatureName):
    raise RuntimeError("feature catalog must cover every FeatureName exactly once")

FEATURE_DEFINITIONS = {item.name: item for item in ORDERED_FEATURES}
PRODUCTION_FEATURES = tuple(
    item.name for item in ORDERED_FEATURES if item.maturity == "production_rule"
)


def feature_schema_sha256() -> str:
    """Return a deterministic digest of the ordered catalog."""

    payload = [
        {
            "name": item.name.value,
            "channel": item.channel,
            "unit": item.unit,
            "source_window_ms": item.source_window_ms,
            "transform_version": item.transform_version,
            "maturity": item.maturity,
            "production_use": item.production_use,
            "exclusion_reason": item.exclusion_reason,
        }
        for item in ORDERED_FEATURES
    ]
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return sha256(encoded).hexdigest()


def to_ordered_array(
    vector: FeatureVector,
    *,
    expected_dimension: int,
) -> tuple[list[float], list[bool]]:
    """Project named values in catalog order and validate exact dimension.

    Missing values are represented by ``0.0`` only alongside a parallel mask;
    consumers must use both arrays. The function refuses implicit dimension
    drift, which is safer than truncating or padding a model input silently.
    """

    if expected_dimension != len(ORDERED_FEATURES):
        raise ValueError(
            f"model expects {expected_dimension} features but schema "
            f"{FEATURE_SCHEMA_VERSION} defines {len(ORDERED_FEATURES)}"
        )
    values: list[float] = []
    mask: list[bool] = []
    for definition in ORDERED_FEATURES:
        item: FeatureValue | None = vector.features.get(definition.name)
        valid = item is not None and item.valid and item.value is not None
        value = (
            float(item.value)
            if valid and item is not None and item.value is not None
            else 0.0
        )
        values.append(value)
        mask.append(valid)
    return values, mask
