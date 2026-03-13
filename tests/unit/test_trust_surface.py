"""Tests for trust surface (causal explanations + consent level)."""

from cortex.libs.schemas.intervention import InterventionPlan, UIPlan


class TestTrustSurface:
    def test_causal_explanation_field(self):
        plan = InterventionPlan(
            level="overlay_only",
            situation_summary="User is overwhelmed",
            headline="Take a step back",
            primary_focus="Current error",
            micro_steps=["Fix the import"],
            ui_plan=UIPlan(),
            causal_explanation="Heart rate rose 18% while switching between 4 tabs in 45 seconds",
        )
        assert "Heart rate" in plan.causal_explanation
        assert plan.is_valid

    def test_consent_level_field(self):
        plan = InterventionPlan(
            level="simplified_workspace",
            situation_summary="Test",
            headline="Test headline",
            primary_focus="Test",
            micro_steps=["Step 1"],
            ui_plan=UIPlan(),
            consent_level="preview",
        )
        assert plan.consent_level == "preview"

    def test_default_consent_level(self):
        plan = InterventionPlan(
            level="overlay_only",
            situation_summary="Test",
            headline="Test",
            primary_focus="Test",
            micro_steps=["Step"],
            ui_plan=UIPlan(),
        )
        assert plan.consent_level == "suggest"

    def test_all_consent_levels(self):
        for level in ["observe", "suggest", "preview", "reversible_act", "autonomous_act"]:
            plan = InterventionPlan(
                level="overlay_only",
                situation_summary="Test",
                headline="Test",
                primary_focus="Test",
                micro_steps=["Step"],
                ui_plan=UIPlan(),
                consent_level=level,
            )
            assert plan.consent_level == level

    def test_causal_explanation_default_empty(self):
        plan = InterventionPlan(
            level="overlay_only",
            situation_summary="Test",
            headline="Test",
            primary_focus="Test",
            micro_steps=["Step"],
            ui_plan=UIPlan(),
        )
        assert plan.causal_explanation == ""
