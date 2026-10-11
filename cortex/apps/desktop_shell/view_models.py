"""Qt-free presentation models for the desktop dashboard."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from typing import Any

from cortex.apps.desktop_shell.tokens import STATE_LABELS


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try:
        candidate = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return candidate if isfinite(candidate) else default


def _measurement(value: object, *, positive: bool = False) -> float | None:
    """Missing and malformed measurements remain unavailable, never zero."""
    candidate = _number(value, float("nan"))
    if not isfinite(candidate) or candidate < 0 or (positive and candidate == 0):
        return None
    return candidate


def _pulse(value: object) -> float | None:
    candidate = _measurement(value, positive=True)
    # Same supported range as the physiological transport contract.
    return candidate if candidate is not None and 30.0 <= candidate <= 220.0 else None


@dataclass(frozen=True, slots=True)
class ConsumerStateViewModel:
    state: str
    label: str
    health_message: str | None
    heart_rate: float | None
    hrv_rmssd: float | None
    blink_rate: float | None
    biometrics_status: str | None
    connected_surfaces: frozenset[str]


_PULSE_MISSING_REASON_COPY: dict[str, str] = {
    "no_face": "Stay in view for a pulse reading",
    "low_light": "Too dark for a pulse reading — add some light",
    "saturated": "Too bright for a pulse reading — reduce glare",
    "motion": "Hold still for a pulse reading",
    "occluded": "Face partly covered — clear the camera's view",
    "camera_warmup": "Camera warming up…",
    "frame_dropped": "Camera frames are dropping — close other camera apps",
    "permission": "Camera permission needed for a pulse reading",
    "source_disconnected": "Camera disconnected",
}


def pulse_unavailable_copy(reason: Any) -> str:
    """Turn a ``PulseReadinessReason`` payload into calm consumer copy.

    v0.4.0 (audit S10): the daemon now says *why* a pulse is unavailable; a
    permanent "Reading your pulse…" was a lie whenever the window could never
    become ready (low light, frequent face loss, sub-sampled capture).
    """

    default = "Reading your pulse…"
    if not isinstance(reason, Mapping):
        return default
    code = str(reason.get("code") or "")
    missing = str(reason.get("missing_reason") or "").lower()
    if code == "filling":
        observed = _measurement(reason.get("observed"))
        required = _measurement(reason.get("required"), positive=True)
        if observed is not None and required is not None:
            return (
                f"Reading your pulse… {int(min(observed, required) + 0.5)} "
                f"of {int(required + 0.5)} s"
            )
        return default
    if code in {"no_observations", "duplicate_timestamps", "too_few_valid_samples"}:
        return default if code != "no_observations" else "Waiting for the camera…"
    if code == "motion_fraction_above_cap":
        return _PULSE_MISSING_REASON_COPY["motion"]
    if missing in _PULSE_MISSING_REASON_COPY:
        return _PULSE_MISSING_REASON_COPY[missing]
    if code == "gap_too_long":
        return _PULSE_MISSING_REASON_COPY["no_face"]
    if code == "valid_fraction_below_gate":
        return "Not enough usable frames yet — stay in view with steady light"
    return default


def consumer_state_view(payload: Mapping[str, Any]) -> ConsumerStateViewModel:
    capture = _mapping(payload.get("capture"))
    store = _mapping(payload.get("store"))
    if bool(capture.get("stale", False)):
        health = "Camera offline — frames are not flowing"
    elif bool(store.get("degraded", False)):
        health = "Storage degraded — sessions may not persist"
    else:
        health = None

    status = str(payload.get("status", "insufficient_evidence"))
    raw_state = str(payload.get("state", "UNKNOWN"))
    state = raw_state if status == "estimated" else "UNKNOWN"
    if status == "warming_up":
        label = "Still gathering"
    elif status == "insufficient_evidence":
        label = "Not enough evidence"
    else:
        label = STATE_LABELS.get(state, "Status unavailable")

    biometrics = _mapping(payload.get("biometrics"))
    raw_hr = biometrics.get("heart_rate")
    heart_rate = _pulse(raw_hr)
    raw_hrv = biometrics.get("hrv_rmssd")
    hrv = _measurement(raw_hrv)
    raw_blink = biometrics.get("blink_rate")
    blink = _measurement(raw_blink)
    if (
        capture.get("stale") or capture.get("frames_flowing") is False
        or capture.get("face_detected") is False
    ):
        heart_rate, hrv, blink = None, None, None
    if capture.get("pulse_unavailable"):
        heart_rate, hrv = None, None
    if heart_rate is None:
        if not bool(capture.get("frames_flowing", True)):
            bio_status = (
                "Camera offline — check System Settings → Privacy "
                "& Security → Camera"
            )
        elif capture.get("stale"):
            bio_status = "Camera signal paused — waiting for fresh frames"
        elif not bool(capture.get("face_detected", True)):
            bio_status = "Looking for your face…"
        else:
            bio_status = pulse_unavailable_copy(capture.get("pulse_unavailable"))
    else:
        bio_status = None

    raw_clients = payload.get("connected_clients")
    connected = frozenset(
        str(value).lower()
        for value in raw_clients
        if isinstance(value, str)
    ) if isinstance(raw_clients, list) else frozenset()
    return ConsumerStateViewModel(
        state=state,
        label=label,
        health_message=health,
        heart_rate=heart_rate,
        hrv_rmssd=hrv,
        blink_rate=blink,
        biometrics_status=bio_status,
        connected_surfaces=connected,
    )


@dataclass(frozen=True, slots=True)
class AdvancedStateViewModel:
    state: str
    scores: Mapping[str, float]
    signal_quality: Mapping[str, Any]
    evidence_strength: float
    evidence_coverage: float
    dwell_seconds: float
    heart_rate: float | None
    degraded_message: str | None


def advanced_state_view(payload: Mapping[str, Any]) -> AdvancedStateViewModel:
    canonical = _mapping(payload.get("support_scores"))
    legacy = _mapping(payload.get("scores"))
    scores = {
        "flow": _number(canonical.get("flow_like", legacy.get("flow"))),
        "hyper": _number(canonical.get("support_likely", legacy.get("hyper"))),
        "hypo": _number(canonical.get("under_engaged", legacy.get("hypo"))),
        "recovery": _number(canonical.get("recovering", legacy.get("recovery"))),
    }
    scores = {key: max(0.0, min(1.0, value)) for key, value in scores.items()}
    signal_quality = dict(_mapping(payload.get("signal_quality")))
    for channel in ("physio", "kinematics", "telemetry"):
        signal_quality[channel] = max(0.0, min(1.0, _number(signal_quality.get(channel))))
    status = str(payload.get("status", "insufficient_evidence"))
    source = payload.get("source")
    if source == "fallback":
        degraded = "Inference unavailable — safety fallback active"
    elif status == "warming_up":
        degraded = "Gathering enough activity evidence"
    elif status == "insufficient_evidence":
        degraded = "Not enough evidence for an estimate"
    else:
        degraded = None
    raw_hr = _mapping(payload.get("biometrics")).get("heart_rate")
    capture = _mapping(payload.get("capture"))
    capture_current = (
        not capture.get("stale") and capture.get("frames_flowing") is not False
        and capture.get("face_detected") is not False
        and not capture.get("pulse_unavailable")
    )
    return AdvancedStateViewModel(
        state=str(payload.get("state", "UNKNOWN")) if status == "estimated" else "UNKNOWN",
        scores=scores,
        signal_quality=signal_quality,
        evidence_strength=max(0.0, min(1.0, _number(payload.get("confidence")))),
        evidence_coverage=max(
            0.0,
            min(1.0, _number(payload.get("evidence_coverage"))),
        ),
        dwell_seconds=max(0.0, _number(payload.get("dwell_seconds"))),
        heart_rate=_pulse(raw_hr) if capture_current else None,
        degraded_message=degraded,
    )
