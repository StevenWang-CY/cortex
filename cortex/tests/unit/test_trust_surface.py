"""Tests for trust surface — causal explanation in interventions."""
import pytest
from datetime import datetime
from cortex.libs.schemas.intervention import InterventionPlan, InterventionOutcome, UIPlan


class TestTrustSurface:
    def test_causal_explanation_serialization(self):
        plan = InterventionPlan(
            level="guided_mode",
            headline="Break recommended",
            situation_summary="Sustained stress detected",
            primary_focus="Take a break",
            micro_steps=["Take a 5-min walk"],
            ui_plan=UIPlan(),
            causal_explanation="Your stress integral crossed 500 ms*s threshold after 28 minutes of declining HRV.",
        )
        data = plan.model_dump()
        assert "causal_explanation" in data
        restored = InterventionPlan.model_validate(data)
        assert "stress integral" in restored.causal_explanation

    def test_intervention_outcome_with_ratings(self):
        outcome = InterventionOutcome(
            intervention_id="int_123",
            started_at=datetime.now(),
            user_action="engaged",
            helpfulness_score=0.85,
            user_rating="thumbs_up",
        )
        assert outcome.helpfulness_score == 0.85
        assert outcome.user_rating == "thumbs_up"

    def test_intervention_outcome_without_ratings(self):
        """Outcome without explicit rating still works."""
        outcome = InterventionOutcome(
            intervention_id="int_456",
            started_at=datetime.now(),
            user_action="dismissed",
        )
        assert outcome.helpfulness_score is None
        assert outcome.user_rating is None

    def test_consent_level_in_plan(self):
        plan = InterventionPlan(
            level="overlay_only",
            headline="Test",
            situation_summary="Test",
            primary_focus="Test focus",
            micro_steps=["Step 1"],
            ui_plan=UIPlan(),
            consent_level="preview",
        )
        assert plan.consent_level == "preview"

    def test_plan_json_roundtrip(self):
        plan = InterventionPlan(
            level="guided_mode",
            headline="Focus",
            situation_summary="Thrashing detected",
            primary_focus="auth.ts",
            micro_steps=["Close unused tabs", "Focus on auth.ts"],
            ui_plan=UIPlan(),
            causal_explanation="You switched between 6 apps in 30 seconds with mean dwell time of 3.2s.",
            consent_level="suggest",
        )
        json_str = plan.model_dump_json()
        restored = InterventionPlan.model_validate_json(json_str)
        assert restored.causal_explanation == plan.causal_explanation
        assert restored.consent_level == plan.consent_level
