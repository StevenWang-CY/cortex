"""
Unit tests for the Pydantic ``WSMessage`` envelope + ``MessageType`` enum.

Debt-1 closure (audit/findings.md): the WS envelope is the source of
truth for the TypeScript ``WSMessage`` interface emitted by the codegen
pipeline. F45 (typo-bypassed dispatch) closes structurally when every
``type`` literal is enumerated in ``MessageType`` and the legacy
dataclass round-trips to the Pydantic model.

Coverage
--------

1. Round-trip from the legacy dataclass to the Pydantic model and back
   preserves every field (the dataclass stays for one-release backwards
   compat per the migration plan).
2. The Pydantic model rejects unknown ``type`` literals at construction
   time — protects the dispatch site that previously silently bypassed
   unknown types.
3. The wire format (JSON shape) matches between the two — exact field
   set, identical values, even when constructed via the enum vs. the
   string literal.
4. Real session JSONL frames replay through the new model without
   error. We use representative captures (one per outbound message
   type) rather than scanning the live ``storage/sessions/`` tree so
   the test is hermetic.
5. ``use_enum_values=True`` is honoured: the stored ``msg.type`` is
   always a plain string, so existing ``if msg.type == "STATE_UPDATE"``
   dispatch sites keep working.

The dataclass-side tests live in ``test_api_gateway.py::TestWSMessage``
and stay green as-is — the dataclass is preserved in this commit per
the migration plan.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from cortex.libs.schemas.ws_message import WSMessage
from cortex.libs.schemas.ws_message_types import MessageType
from cortex.services.api_gateway.websocket_server import WSMessageLegacy


def test_message_type_catalog_covers_dispatch_arms() -> None:
    """Every literal that the daemon dispatches on lives in the catalog.

    Pinned list mirrors ``_process_message`` in ``websocket_server.py``.
    A regression where a new arm is added without extending the enum
    fails this test before it can ship.
    """
    dispatched_inbound = {
        "USER_ACTION",
        "ACTION_EXECUTE",
        "USER_RATING",
        "IDENTIFY",
        "CONTEXT_RESPONSE",
        "SETTINGS_SYNC",
        "ACTIVITY_SYNC",
        "TAB_RELEVANCE_FEEDBACK",
        "LEETCODE_CONTEXT_UPDATE",
        "INTERVENTION_APPLIED",
        "SHUTDOWN",
    }
    catalog = {m.value for m in MessageType}
    missing = dispatched_inbound - catalog
    assert not missing, f"MessageType missing dispatched literals: {missing}"


def test_message_type_catalog_covers_outbound() -> None:
    """Every literal the daemon emits (``_make_*`` and ``send_message``)
    lives in the catalog."""
    outbound = {
        "STATE_UPDATE",
        "INTERVENTION_TRIGGER",
        "INTERVENTION_RESTORE",
        "SETTINGS_SYNC",
        "CONTEXT_REQUEST",
        "ACTIVE_RECALL",
        "BREATHING_OVERLAY",
        "PRE_BREAK_WARNING",
        "MORNING_BRIEFING",
        "COPILOT_THROTTLE",
        "AMBIENT_STATE_UPDATE",
    }
    catalog = {m.value for m in MessageType}
    missing = outbound - catalog
    assert not missing, f"MessageType missing outbound literals: {missing}"


def test_message_type_catalog_covers_leetcode_adapter_emissions() -> None:
    """The LeetCode adapter emits typed messages through ``send_message``;
    each LEETCODE_* literal it can produce must be enumerated so the
    Pydantic validator does not reject the message at construction time."""
    leetcode_outbound = {
        "LEETCODE_SHOW_SCRATCHPAD",
        "LEETCODE_SHOW_PATTERN_LADDER",
        "LEETCODE_SHOW_LOCKOUT",
        "LEETCODE_SHOW_CONSOLIDATION",
        "LEETCODE_SHOW_SUBMISSION_GATE",
        "LEETCODE_SHOW_SOLUTION_FRICTION",
        "LEETCODE_SHOW_SESSION_BRIEFING",
        "LEETCODE_LOCK_EDITOR",
        "LEETCODE_INTERCEPT_SUBMIT",
        "LEETCODE_GATE_SOLUTIONS",
        "LEETCODE_AI_RESTATEMENT_CHECK",
        "LEETCODE_AI_COMPREHENSION_CHECK",
        "LEETCODE_AI_HYPOTHESIS_CHECK",
        "LEETCODE_AI_STUCK_ANALYSIS",
        "LEETCODE_AI_SESSION_BRIEFING",
    }
    catalog = {m.value for m in MessageType}
    missing = leetcode_outbound - catalog
    assert not missing, f"MessageType missing LeetCode literals: {missing}"


def test_construct_with_string_literal() -> None:
    """Plain-string ``type`` works (existing call sites untouched)."""
    msg = WSMessage(type="STATE_UPDATE", payload={"state": "FLOW"})
    assert msg.type == "STATE_UPDATE"
    # use_enum_values=True keeps msg.type as the wire string.
    assert isinstance(msg.type, str)


def test_construct_with_enum_member() -> None:
    """Enum member ``type`` works (new call sites use this)."""
    msg = WSMessage(type=MessageType.STATE_UPDATE, payload={})
    assert msg.type == "STATE_UPDATE"
    assert isinstance(msg.type, str)


def test_unknown_type_rejected() -> None:
    """A typo in ``type`` raises at construction (F45 closure)."""
    with pytest.raises(ValidationError):
        WSMessage(type="STAT_UPDATE", payload={})  # typo


def test_roundtrip_pydantic_to_pydantic() -> None:
    """``from_json(to_json(x)) == x`` for every field."""
    original = WSMessage(
        type=MessageType.INTERVENTION_TRIGGER,
        payload={"headline": "Focus on one thing"},
        timestamp=200.0,
        sequence=10,
        correlation_id="cid_abc",
        target_client_types=["chrome", "vscode"],
        source_client_type="daemon",
    )
    restored = WSMessage.from_json(original.to_json())
    assert restored.type == original.type
    assert restored.payload == original.payload
    assert restored.timestamp == original.timestamp
    assert restored.sequence == original.sequence
    assert restored.correlation_id == original.correlation_id
    assert restored.target_client_types == original.target_client_types
    assert restored.source_client_type == original.source_client_type


def test_legacy_dataclass_roundtrips_to_pydantic() -> None:
    """``WSMessageLegacy`` serialises to a JSON shape Pydantic accepts."""
    legacy = WSMessageLegacy(
        type="USER_ACTION",
        payload={"action": "dismissed", "intervention_id": "int_xyz"},
        timestamp=100.0,
        sequence=3,
        correlation_id="cid_legacy",
    )
    new = WSMessage.from_json(legacy.to_json())
    assert new.type == "USER_ACTION"
    assert new.payload == legacy.payload
    assert new.sequence == legacy.sequence
    assert new.correlation_id == legacy.correlation_id


def test_legacy_dataclass_to_pydantic_helper() -> None:
    """The explicit ``WSMessageLegacy.to_pydantic`` migration helper works."""
    legacy = WSMessageLegacy(
        type="SETTINGS_SYNC",
        payload={"quiet_mode": True},
        sequence=7,
    )
    new = legacy.to_pydantic()
    assert isinstance(new, WSMessage)
    assert new.type == "SETTINGS_SYNC"
    assert new.payload == {"quiet_mode": True}


def test_wire_format_matches_legacy_field_set() -> None:
    """Wire JSON carries exactly the same keys as the legacy dataclass."""
    pyd = WSMessage(
        type=MessageType.STATE_UPDATE,
        payload={"state": "FLOW"},
        sequence=1,
    )
    legacy = WSMessageLegacy(
        type="STATE_UPDATE",
        payload={"state": "FLOW"},
        sequence=1,
        timestamp=pyd.timestamp,  # eliminate timing skew
    )
    pyd_keys = set(json.loads(pyd.to_json()).keys())
    legacy_keys = set(json.loads(legacy.to_json()).keys())
    assert pyd_keys == legacy_keys


def test_replay_representative_session_frames() -> None:
    """Representative captures of in-flight frames replay cleanly.

    These shapes are sampled from the actual daemon emit sites in
    ``websocket_server.py`` and ``runtime_daemon.py``. Catching a parse
    failure here means the round-trip would break a live session
    replay.
    """
    representative_frames = [
        # STATE_UPDATE (the hottest broadcast).
        json.dumps(
            {
                "type": "STATE_UPDATE",
                "payload": {
                    "state": "FLOW",
                    "confidence": 0.82,
                    "scores": {"flow": 0.82, "hypo": 0.05, "hyper": 0.10, "recovery": 0.03},
                    "signal_quality": {"physio": 0.9, "kinematics": 0.85, "telemetry": 0.95, "overall": 0.9},
                    "dwell_seconds": 12.3,
                    "reasons": [],
                    "stress_integral": 0.05,
                    "calibrated_probabilities": [0.82, 0.05, 0.10, 0.03],
                    "classifier_source": "rules",
                    "classifier_alpha": 1.0,
                    "timestamp": 123456.789,
                },
                "timestamp": 123456.789,
                "sequence": 1,
                "correlation_id": None,
                "target_client_types": None,
                "source_client_type": "daemon",
            }
        ),
        # INTERVENTION_TRIGGER with full plan envelope.
        json.dumps(
            {
                "type": "INTERVENTION_TRIGGER",
                "payload": {
                    "intervention_id": "int_abc",
                    "level": "overlay_only",
                    "headline": "Pick one tab",
                    "situation_summary": "Six tabs open, only one is your code",
                    "primary_focus": "your editor",
                    "micro_steps": ["close the noise", "breathe", "edit"],
                    "hide_targets": [],
                    "ui_plan": {
                        "dim_background": False,
                        "show_overlay": True,
                        "fold_unrelated_code": False,
                        "intervention_type": "overlay_only",
                        "max_visible_lines": 40,
                    },
                    "tone": "supportive",
                    "suggested_actions": [],
                    "causal_explanation": "HRV trended down for 90s",
                    "consent_level": "suggest",
                    "plan_warnings": [],
                },
                "timestamp": 123456.789,
                "sequence": 2,
                "correlation_id": "cid_xyz",
                "target_client_types": None,
                "source_client_type": "daemon",
            }
        ),
        # USER_ACTION (inbound dismiss).
        json.dumps(
            {
                "type": "USER_ACTION",
                "payload": {"action": "dismissed", "intervention_id": "int_abc"},
                "timestamp": 123457.0,
                "sequence": 0,
                "correlation_id": "cid_xyz",
                "target_client_types": None,
                "source_client_type": "chrome",
            }
        ),
        # SHUTDOWN with no payload.
        json.dumps(
            {
                "type": "SHUTDOWN",
                "payload": {},
                "timestamp": 999.0,
                "sequence": 0,
                "correlation_id": None,
                "target_client_types": None,
                "source_client_type": "chrome",
            }
        ),
    ]
    for raw in representative_frames:
        msg = WSMessage.from_json(raw)
        # Every replayed frame must round-trip cleanly.
        assert isinstance(msg, WSMessage)
        # And the parsed type stays string-typed for legacy comparison.
        assert isinstance(msg.type, str)


def test_default_payload_is_empty_dict() -> None:
    """Missing payload normalises to an empty dict (matches legacy behaviour)."""
    msg = WSMessage(type=MessageType.SHUTDOWN)
    assert msg.payload == {}


def test_unknown_keys_are_ignored() -> None:
    """An incoming frame with a forward-compat field doesn't crash parse."""
    raw = json.dumps(
        {
            "type": "STATE_UPDATE",
            "payload": {},
            "sequence": 0,
            "future_field": "ignored",
        }
    )
    msg = WSMessage.from_json(raw)
    # Pydantic ConfigDict(extra="ignore") drops the unknown field silently.
    assert not hasattr(msg, "future_field")
