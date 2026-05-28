"""P1-11: StateEstimate.state uses UserState enum with use_enum_values=True.

Verifies:
- model_dump_json() emits ``"state":"FLOW"`` (string, not enum repr).
- model_json_schema() still emits a literal-string-union so TS codegen output
  is byte-identical.
- StateEstimate can be constructed with UserState enum values OR plain strings.
"""

from __future__ import annotations

import json

from cortex.libs.schemas.state import (
    SignalQuality,
    StateEstimate,
    StateScores,
    UserState,
)


def _make_estimate(state: UserState | str = UserState.FLOW) -> StateEstimate:
    return StateEstimate(
        state=state,
        confidence=0.9,
        scores=StateScores(flow=0.9, hypo=0.0, hyper=0.0, recovery=0.1),
        signal_quality=SignalQuality(physio=0.8, kinematics=0.7, telemetry=0.9),
        timestamp=1_700_000_000.0,
    )


class TestStateEstimateEnumSerialization:
    def test_model_dump_json_state_is_string(self) -> None:
        """model_dump_json must emit the plain string value, not the enum repr."""
        est = _make_estimate(UserState.FLOW)
        dumped = json.loads(est.model_dump_json())
        assert dumped["state"] == "FLOW", (
            f"Expected 'FLOW', got {dumped['state']!r}"
        )

    def test_model_dump_mode_json_state_is_string(self) -> None:
        dumped = _make_estimate(UserState.HYPER).model_dump(mode="json")
        assert dumped["state"] == "HYPER"

    def test_model_dump_mode_python_state_is_string_due_to_use_enum_values(self) -> None:
        """use_enum_values=True means model_dump() returns the string value."""
        dumped = _make_estimate(UserState.HYPO).model_dump()
        # With use_enum_values=True the Python dict also contains a string.
        assert dumped["state"] == "HYPO"

    def test_construct_with_enum_value(self) -> None:
        est = _make_estimate(UserState.RECOVERY)
        assert est.state == "RECOVERY"  # string due to use_enum_values

    def test_construct_with_plain_string(self) -> None:
        est = _make_estimate("FLOW")
        assert est.state == "FLOW"

    def test_all_states_serialize_correctly(self) -> None:
        for state in (UserState.FLOW, UserState.HYPO, UserState.HYPER, UserState.RECOVERY):
            est = _make_estimate(state)
            dumped = json.loads(est.model_dump_json())
            assert dumped["state"] == state.value

    def test_json_schema_state_is_literal_string_union(self) -> None:
        """model_json_schema() must emit a literal-string-union for 'state'.

        This is required for TS codegen to produce
        ``"FLOW" | "HYPO" | "HYPER" | "RECOVERY"`` unions.
        """
        schema = StateEstimate.model_json_schema()
        state_schema = schema["properties"]["state"]
        # With use_enum_values=True on a StrEnum, Pydantic emits
        # {"enum": ["FLOW", ...]} or {"$ref": "..."} — either way the
        # resolved schema must contain the four string literals.
        # Resolve $ref if present.
        if "$ref" in state_schema:
            ref_name = state_schema["$ref"].split("/")[-1]
            state_schema = schema["$defs"][ref_name]

        enum_values = state_schema.get("enum") or []
        assert set(enum_values) == {"FLOW", "HYPO", "HYPER", "RECOVERY"}, (
            f"Expected literal union, got schema: {state_schema}"
        )
        # All values must be plain strings (not e.g. {"value": "FLOW"}).
        for v in enum_values:
            assert isinstance(v, str), f"enum value {v!r} is not a string"

    def test_is_overwhelmed_property_works_after_enum_migration(self) -> None:
        est = _make_estimate(UserState.HYPER)
        assert est.is_overwhelmed is True

    def test_is_flow_property_works_after_enum_migration(self) -> None:
        est = _make_estimate(UserState.FLOW)
        assert est.is_flow is True
