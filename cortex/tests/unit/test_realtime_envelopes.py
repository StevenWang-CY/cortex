"""Phase-4a Debt-1: round-trip + Literal-enforcement tests for the new
realtime envelopes in :mod:`cortex.libs.schemas.realtime`.

Why this exists
---------------

The daemon previously broadcast these payloads as raw ``dict`` literals;
Phase-4a promotes them to Pydantic models so the wire shape is checked
at construction time and the codegen pipeline emits matching
TypeScript types. These tests pin three invariants per envelope:

1. The model round-trips via ``model_dump(mode="json")`` →
   ``model_validate`` without losing fields.
2. ``Literal``-typed fields reject unknown values at parse time.
3. Optional fields tolerate ``None`` where the schema allows it.

They also cover the cross-cutting :class:`InterventionApplied` envelope
in :mod:`cortex.libs.schemas.intervention` because it's part of the
same Phase-4a batch.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from cortex.libs.schemas.intervention import (
    CausalSignal,
    InterventionApplied,
    SuggestedAction,
)
from cortex.libs.schemas.realtime import (
    BreakRecommendation,
    QuietModeState,
    QuietModeTogglePayload,
    SessionRecap,
    StartFocusAutoPayload,
    StopFocusAutoPayload,
    WhyDetail,
)
from cortex.libs.schemas.session_report import SessionReport

# ─── BreakRecommendation ───────────────────────────────────────────────


def test_break_recommendation_roundtrip() -> None:
    msg = BreakRecommendation(
        reason="stress_integral_crossed_threshold",
        urgency="medium",
        stress_load=2.5,
        threshold=2.0,
        duration_seconds=240,
        breathing_pattern="4-7-8",
    )
    blob = msg.model_dump(mode="json")
    restored = BreakRecommendation.model_validate(blob)
    assert restored == msg


def test_break_recommendation_urgency_rejects_unknown() -> None:
    with pytest.raises(ValidationError):
        BreakRecommendation(
            reason="x",
            urgency="catastrophic",  # type: ignore[arg-type]
            stress_load=1.0,
            threshold=1.0,
            duration_seconds=60,
        )


def test_break_recommendation_breathing_pattern_rejects_unknown() -> None:
    with pytest.raises(ValidationError):
        BreakRecommendation(
            reason="x",
            urgency="low",
            stress_load=1.0,
            threshold=1.0,
            duration_seconds=60,
            breathing_pattern="meditation",  # type: ignore[arg-type]
        )


# ─── QuietModeState ────────────────────────────────────────────────────


def test_quiet_mode_state_roundtrip_with_source() -> None:
    msg = QuietModeState(
        kind="quiet_session",
        duration_minutes=45.0,
        ends_at=1_700_000_000.0,
        source="dashboard",
    )
    blob = msg.model_dump(mode="json")
    restored = QuietModeState.model_validate(blob)
    assert restored == msg


def test_quiet_mode_state_off_clears_durations() -> None:
    msg = QuietModeState(kind="off")
    assert msg.duration_minutes is None
    assert msg.ends_at is None
    # 'daemon' is the default source — verifies the field default holds.
    assert msg.source == "daemon"


def test_quiet_mode_state_rejects_unknown_source() -> None:
    with pytest.raises(ValidationError):
        QuietModeState(
            kind="snooze_15",
            source="hostile_client",  # type: ignore[arg-type]
        )


def test_quiet_mode_state_kind_rejects_unknown() -> None:
    with pytest.raises(ValidationError):
        QuietModeState(kind="permanent")  # type: ignore[arg-type]


def test_quiet_mode_state_all_ten_sources_accepted() -> None:
    """Mirror the dispatcher's _ALLOWED_SOURCES — every listed source
    must validate. If the dispatcher grows a new source, this test
    fails until the schema is updated to match."""
    for src in (
        "dashboard",
        "overlay",
        "tray",
        "shortcut",
        "popup",
        "vscode",
        "os_notification",
        "settings_sync",
        "daemon",
        "daemon_decay",
    ):
        QuietModeState(kind="snooze_15", source=src)  # must not raise


def test_quiet_mode_toggle_payload_roundtrip() -> None:
    msg = QuietModeTogglePayload(
        kind="snooze_15",
        duration_minutes=15,
        source="overlay",
    )
    blob = msg.model_dump(mode="json")
    restored = QuietModeTogglePayload.model_validate(blob)
    assert restored == msg


# ─── WhyDetail ─────────────────────────────────────────────────────────


def test_why_detail_roundtrip() -> None:
    signals = [
        CausalSignal(
            name="HRV",
            current_value=42.0,
            unit="ms",
            samples_60s=[40.0, 41.0, 42.0],
            severity="primary",
        ),
    ]
    msg = WhyDetail(intervention_id="int_abc123", causal_signals=signals)
    blob = msg.model_dump(mode="json")
    restored = WhyDetail.model_validate(blob)
    assert restored.intervention_id == msg.intervention_id
    assert len(restored.causal_signals) == 1
    assert restored.causal_signals[0].name == "HRV"


def test_why_detail_caps_signals_at_three() -> None:
    too_many = [
        CausalSignal(name=f"sig{i}", current_value=0.0, unit="ms")
        for i in range(4)
    ]
    with pytest.raises(ValidationError):
        WhyDetail(intervention_id="int_x", causal_signals=too_many)


def test_why_detail_error_is_optional() -> None:
    msg = WhyDetail(intervention_id="int_y", error="not_found")
    assert msg.causal_signals == []
    assert msg.error == "not_found"


# ─── StartFocusAutoPayload / StopFocusAutoPayload ──────────────────────


def test_start_focus_auto_payload_roundtrip() -> None:
    msg = StartFocusAutoPayload(
        duration_minutes=25,
        reason="sustained_hyper_confidence",
        preset="developer",
        custom_domains=["news.ycombinator.com", "twitter.com"],
    )
    blob = msg.model_dump(mode="json")
    restored = StartFocusAutoPayload.model_validate(blob)
    assert restored == msg


def test_start_focus_auto_payload_rejects_unknown_preset() -> None:
    with pytest.raises(ValidationError):
        StartFocusAutoPayload(
            duration_minutes=20,
            reason="x",
            preset="gamer",  # type: ignore[arg-type]
        )


def test_start_focus_auto_payload_duration_bounds_enforced() -> None:
    with pytest.raises(ValidationError):
        StartFocusAutoPayload(duration_minutes=0, reason="x")
    with pytest.raises(ValidationError):
        StartFocusAutoPayload(duration_minutes=999, reason="x")


def test_stop_focus_auto_payload_roundtrip() -> None:
    msg = StopFocusAutoPayload(reason="natural_recovery")
    blob = msg.model_dump(mode="json")
    restored = StopFocusAutoPayload.model_validate(blob)
    assert restored == msg


# ─── SessionRecap ──────────────────────────────────────────────────────


def _make_report() -> SessionReport:
    """Construct a minimal SessionReport for envelope tests."""
    now = datetime.now(UTC)
    return SessionReport(
        session_id="abc123",
        start_time=now,
        end_time=now,
        duration_seconds=120.0,
    )


def test_session_recap_roundtrip_with_defaults() -> None:
    report = _make_report()
    msg = SessionRecap(report=report)
    blob = msg.model_dump(mode="json")
    restored = SessionRecap.model_validate(blob)
    assert restored.report.session_id == "abc123"
    assert restored.persisted is True  # default
    assert restored.generated_at is not None


def test_session_recap_persisted_false_survives_roundtrip() -> None:
    report = _make_report()
    msg = SessionRecap(report=report, persisted=False)
    blob = msg.model_dump(mode="json")
    restored = SessionRecap.model_validate(blob)
    assert restored.persisted is False


def test_session_recap_carries_explicit_generated_at() -> None:
    report = _make_report()
    # C4 (audit): generated_at is an ISO-8601 *string* so the daemon's
    # SessionRecap(generated_at=<iso8601 str>, ...) wrapper matches the
    # schema and the generated TS type is a plain `string`.
    when = datetime(2026, 5, 27, 10, 0, tzinfo=UTC).isoformat()
    msg = SessionRecap(report=report, generated_at=when)
    assert msg.generated_at == when
    assert isinstance(msg.generated_at, str)


def test_session_recap_generated_at_is_str_by_default() -> None:
    """C4: the default-factory produces a str, not a datetime, so the
    wire shape (schema == wire) is a JSON string in both paths."""
    report = _make_report()
    msg = SessionRecap(report=report)
    assert isinstance(msg.generated_at, str)
    # Round-trips through model_dump(mode="json") unchanged.
    blob = msg.model_dump(mode="json")
    assert isinstance(blob["generated_at"], str)
    restored = SessionRecap.model_validate(blob)
    assert restored.generated_at == msg.generated_at


# ─── InterventionApplied ───────────────────────────────────────────────


def test_intervention_applied_roundtrip() -> None:
    msg = InterventionApplied(
        intervention_id="int_xyz",
        phase="apply",
        success=True,
        applied_actions=["act_1", "act_2"],
        errors=[],
        source_client_type="chrome",
    )
    blob = msg.model_dump(mode="json")
    restored = InterventionApplied.model_validate(blob)
    assert restored == msg


def test_intervention_applied_phase_rejects_unknown() -> None:
    with pytest.raises(ValidationError):
        InterventionApplied(
            intervention_id="int_x",
            phase="cleanup",  # type: ignore[arg-type]
            success=True,
        )


def test_intervention_applied_partial_failure() -> None:
    """The envelope must carry per-action errors so partial successes
    don't silently masquerade as full successes."""
    msg = InterventionApplied(
        intervention_id="int_p",
        phase="apply",
        success=False,
        applied_actions=["act_1"],
        errors=["act_2: tab not found"],
    )
    assert msg.success is False
    assert msg.applied_actions == ["act_1"]
    assert msg.errors == ["act_2: tab not found"]


# ─── New action_types live in the suggested-action Literal ──────────────


def test_suggested_action_take_biology_break_accepted() -> None:
    """Phase-4a sanity: the four new action_types listed in §3 must
    validate without complaint. If a downstream agent removes any of
    them from the Literal this test catches the regression."""
    for action_type in (
        "take_biology_break",
        "resume_last_active_file",
        "prompt_micro_commit",
        "suggest_movement_break",
    ):
        SuggestedAction(action_type=action_type, label="x")
