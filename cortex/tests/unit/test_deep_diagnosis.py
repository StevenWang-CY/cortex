"""Tests for deep bottleneck diagnosis — extended ErrorAnalysis schema."""
import pytest
from cortex.libs.schemas.intervention import ErrorAnalysis, InterventionPlan, UIPlan


class TestDeepDiagnosis:
    def test_error_analysis_extended_fields(self):
        analysis = ErrorAnalysis(
            error_type="TypeError",
            root_cause="Accessing property on undefined object",
            suggested_fix="Add null check: user?.name?.length ?? 0",
            failing_abstraction="user property access",
            symbol_location="src/auth.ts:42:user.name",
            root_cause_category="null_reference",
            minimal_edit="Add null check: user?.name?.length ?? 0",
        )
        assert analysis.failing_abstraction == "user property access"
        assert analysis.root_cause_category == "null_reference"
        assert "null check" in analysis.minimal_edit

    def test_error_analysis_optional_fields(self):
        """Extended fields are optional for backwards compat."""
        analysis = ErrorAnalysis(
            error_type="SyntaxError",
            root_cause="Unexpected token in source",
        )
        assert analysis.failing_abstraction == ""
        assert analysis.root_cause_category == "other"

    def test_intervention_plan_causal_explanation(self):
        plan = InterventionPlan(
            level="guided_mode",
            headline="Take a break",
            situation_summary="Your HRV has been declining for 30 minutes",
            primary_focus="Recovery from cognitive fatigue",
            micro_steps=["Stand up", "Stretch", "Look away from screen"],
            ui_plan=UIPlan(),
            causal_explanation="Your heart rate variability dropped 40% below baseline while error rate increased, indicating cognitive fatigue.",
            consent_level="suggest",
        )
        assert "heart rate variability" in plan.causal_explanation
        assert plan.consent_level == "suggest"

    def test_intervention_plan_backwards_compat(self):
        """Plans without v2.0 fields still work."""
        plan = InterventionPlan(
            level="overlay_only",
            headline="Focus mode",
            situation_summary="High context switching",
            primary_focus="Current task",
            micro_steps=["Close tabs"],
            ui_plan=UIPlan(),
        )
        # causal_explanation should default to empty
        assert plan.causal_explanation == ""
