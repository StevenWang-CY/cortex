"""Phase-4 Debt-1: contract tests for :class:`StateUpdatePayload` and
:class:`InterventionTriggerPayload` in :mod:`cortex.libs.schemas.realtime`.

These cover the two payloads ``websocket_server._make_state_update`` and
``_make_intervention_trigger`` now build via Pydantic instead of raw
``dict`` literals. The tests pin three invariants per envelope:

1. The payload round-trips via ``model_dump(mode="json")`` →
   ``model_validate`` without losing fields.
2. The capture / store / biometrics sub-shapes nest correctly when
   serialised.
3. Literal-typed fields (``state``, ``source``) reject unknown values.

The InterventionTriggerPayload tests additionally pin the wire-shape
backward-compat choice: ``payload.intervention_id`` resolves directly,
no nested ``payload.plan.intervention_id`` indirection. The two
envelope-stamp fields (``desktop_not_focused``, ``connected_clients``)
are appended onto the InterventionPlan extension as defaults-None
optionals so older callers constructing bare ``InterventionPlan``
remain valid.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cortex.libs.schemas.intervention import (
    InterventionPlan,
    MicroStep,
    UIPlan,
)
from cortex.libs.schemas.realtime import (
    BiometricsSummary,
    CaptureStatus,
    InterventionTriggerPayload,
    StateUpdatePayload,
    StoreHealth,
)
from cortex.libs.schemas.state import SignalQuality, StateScores

# ─── CaptureStatus ─────────────────────────────────────────────────────


def test_capture_status_defaults() -> None:
    s = CaptureStatus()
    assert s.frames_flowing is False
    assert s.face_detected is False
    assert s.stale is False
    assert s.sequence is None


def test_capture_status_roundtrip() -> None:
    s = CaptureStatus(
        frames_flowing=True,
        face_detected=True,
        stale=False,
        sequence=42,
    )
    blob = s.model_dump(mode="json")
    restored = CaptureStatus.model_validate(blob)
    assert restored == s


# ─── StoreHealth ───────────────────────────────────────────────────────


def test_store_health_defaults() -> None:
    s = StoreHealth()
    assert s.degraded is False
    assert s.backend is None
    assert s.healthy is None


def test_store_health_roundtrip() -> None:
    s = StoreHealth(degraded=True, backend="in_memory", healthy=True)
    blob = s.model_dump(mode="json")
    restored = StoreHealth.model_validate(blob)
    assert restored == s


# ─── BiometricsSummary ─────────────────────────────────────────────────


def test_biometrics_summary_all_optional() -> None:
    """Every field is ``float | None`` — an empty bundle must parse."""
    s = BiometricsSummary()
    blob = s.model_dump(mode="json")
    restored = BiometricsSummary.model_validate(blob)
    assert restored == s
    assert s.heart_rate is None
    assert s.hrv_rmssd is None
    assert s.respiration_rate is None


def test_biometrics_summary_roundtrip_full() -> None:
    s = BiometricsSummary(
        heart_rate=72.0,
        hrv_rmssd=48.5,
        hr_delta=-3.2,
        blink_rate=18.0,
        perclos=0.12,
        forward_lean=0.32,
        forward_lean_angle=14.4,
        respiration_rate=14.5,
        thrashing_score=0.15,
        stress_integral=2.5,
    )
    blob = s.model_dump(mode="json")
    restored = BiometricsSummary.model_validate(blob)
    assert restored == s


def test_biometrics_summary_extra_keys_ignored() -> None:
    """The producer's ``biometrics`` dict has historical extras (e.g.
    ``stress_integral`` predates the schema). ``extra='ignore'`` lets
    new keys flow through without bumping the schema each time."""
    s = BiometricsSummary.model_validate(
        {
            "heart_rate": 72.0,
            "future_signal": 1.23,  # silently ignored
        }
    )
    assert s.heart_rate == 72.0


# ─── StateUpdatePayload ────────────────────────────────────────────────


def _typical_state_payload() -> StateUpdatePayload:
    return StateUpdatePayload(
        state="FLOW",
        confidence=0.82,
        scores=StateScores(flow=0.82, hypo=0.05, hyper=0.10, recovery=0.03),
        signal_quality=SignalQuality(physio=0.9, kinematics=0.8, telemetry=0.7),
        dwell_seconds=42.5,
        reasons=["hrv_stable", "low_thrashing"],
        stress_integral=1.2,
        calibrated_probabilities=None,
        classifier_source="ensemble",
        classifier_alpha=0.7,
        source="classifier",
        degraded=False,
        timestamp=1_700_000_000.0,
        connected_clients=["chrome", "vscode"],
        capture=CaptureStatus(frames_flowing=True, face_detected=True),
        store=StoreHealth(degraded=False),
        biometrics=BiometricsSummary(heart_rate=72.0, hrv_rmssd=48.5),
        sequence=12_345,
    )


def test_state_update_payload_roundtrip() -> None:
    msg = _typical_state_payload()
    blob = msg.model_dump(mode="json")
    restored = StateUpdatePayload.model_validate(blob)
    assert restored == msg


def test_state_update_payload_state_literal_rejects_unknown() -> None:
    with pytest.raises(ValidationError):
        StateUpdatePayload(
            state="BOREDOM",  # type: ignore[arg-type]
            confidence=0.5,
            scores=StateScores(),
            signal_quality=SignalQuality(),
        )


def test_state_update_payload_source_literal_rejects_unknown() -> None:
    with pytest.raises(ValidationError):
        StateUpdatePayload(
            state="FLOW",
            confidence=0.5,
            scores=StateScores(),
            signal_quality=SignalQuality(),
            source="hallucinated",  # type: ignore[arg-type]
        )


def test_state_update_payload_timestamp_accepts_iso_string() -> None:
    """The legacy producer may emit an ISO string for ``timestamp`` when
    ``StateEstimate.timestamp`` is a datetime; the wire shape must
    tolerate both."""
    msg = StateUpdatePayload(
        state="FLOW",
        confidence=0.5,
        scores=StateScores(),
        signal_quality=SignalQuality(),
        timestamp="2026-05-28T12:00:00+00:00",
    )
    assert isinstance(msg.timestamp, str)


def test_state_update_payload_biometrics_optional() -> None:
    """An empty bundle must parse with biometrics=None (the producer
    omits the key entirely when there are no values to share)."""
    msg = StateUpdatePayload(
        state="FLOW",
        confidence=0.5,
        scores=StateScores(),
        signal_quality=SignalQuality(),
    )
    assert msg.biometrics is None
    blob = msg.model_dump(mode="json")
    assert blob["biometrics"] is None


def test_state_update_payload_defaults_capture_and_store() -> None:
    """Capture / store default to safe ('all-false') states so a partial
    construction by a test or fallback path doesn't blow up."""
    msg = StateUpdatePayload(
        state="HYPO",
        confidence=0.1,
        scores=StateScores(),
        signal_quality=SignalQuality(),
    )
    assert msg.capture.frames_flowing is False
    assert msg.store.degraded is False


# ─── InterventionTriggerPayload ────────────────────────────────────────


def _typical_plan() -> InterventionPlan:
    return InterventionPlan(
        level="overlay_only",
        situation_summary="High stress sustained for 3 minutes.",
        headline="Take a breath",
        primary_focus="Slow down and review the error",
        micro_steps=[MicroStep(text="Pause and breathe for 30 seconds")],
        ui_plan=UIPlan(),
    )


def test_intervention_trigger_payload_roundtrip_with_stamps() -> None:
    plan = _typical_plan()
    plan_dict = plan.model_dump(mode="json")
    plan_dict["desktop_not_focused"] = True
    plan_dict["connected_clients"] = ["chrome", "desktop"]
    msg = InterventionTriggerPayload.model_validate(plan_dict)
    assert msg.desktop_not_focused is True
    assert msg.connected_clients == ["chrome", "desktop"]
    assert msg.intervention_id == plan.intervention_id  # FLAT wire shape

    # Round-trip the wire shape.
    wire = msg.model_dump(mode="json")
    restored = InterventionTriggerPayload.model_validate(wire)
    assert restored.intervention_id == plan.intervention_id
    assert restored.desktop_not_focused is True


def test_intervention_trigger_payload_stamps_default_none() -> None:
    """A bare-plan construction (no stamps) leaves both fields None so
    a forward-compatible older daemon never accidentally fires the
    OS-notification path."""
    plan = _typical_plan()
    msg = InterventionTriggerPayload.model_validate(
        plan.model_dump(mode="json")
    )
    assert msg.desktop_not_focused is None
    assert msg.connected_clients is None


def test_intervention_trigger_payload_inherits_plan_fields() -> None:
    """The flat-wire choice means ``payload.intervention_id``,
    ``payload.micro_steps``, etc., resolve directly without a
    ``payload.plan.`` prefix."""
    plan = _typical_plan()
    msg = InterventionTriggerPayload.model_validate(
        plan.model_dump(mode="json")
    )
    assert msg.headline == "Take a breath"
    assert len(msg.micro_steps) == 1
    assert msg.ui_plan.show_overlay is True


def test_intervention_trigger_payload_extra_keys_ignored() -> None:
    """Forward-compat: a future daemon may stamp additional envelope
    fields the older client doesn't know about. ``extra='ignore'`` keeps
    the older client parsing rather than crashing."""
    plan = _typical_plan()
    blob = plan.model_dump(mode="json")
    blob["future_envelope_field"] = "ignored"
    msg = InterventionTriggerPayload.model_validate(blob)
    assert msg.headline == "Take a breath"
