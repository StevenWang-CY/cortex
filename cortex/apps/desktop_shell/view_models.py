"""Qt-free presentation models for the desktop dashboard."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cortex.apps.desktop_shell.tokens import STATE_LABELS


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try:
        candidate = float(value)
    except (TypeError, ValueError):
        return default
    return candidate if candidate == candidate else default


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
    heart_rate = _number(raw_hr) if raw_hr is not None else None
    raw_hrv = biometrics.get("hrv_rmssd")
    hrv = _number(raw_hrv) if raw_hrv is not None else None
    raw_blink = biometrics.get("blink_rate")
    blink = _number(raw_blink) if raw_blink is not None else None
    if heart_rate is None:
        if not bool(capture.get("frames_flowing", True)):
            bio_status = (
                "Camera offline — check System Settings → Privacy "
                "& Security → Camera"
            )
        elif not bool(capture.get("face_detected", True)):
            bio_status = "Looking for your face…"
        else:
            bio_status = "Reading your pulse…"
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
    return AdvancedStateViewModel(
        state=str(payload.get("state", "UNKNOWN")),
        scores=scores,
        signal_quality=_mapping(payload.get("signal_quality")),
        evidence_strength=max(0.0, min(1.0, _number(payload.get("confidence")))),
        evidence_coverage=max(
            0.0,
            min(1.0, _number(payload.get("evidence_coverage"))),
        ),
        dwell_seconds=max(0.0, _number(payload.get("dwell_seconds"))),
        heart_rate=_number(raw_hr) if raw_hr is not None else None,
        degraded_message=degraded,
    )
