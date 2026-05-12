"""Tests for Cortex v2.0 schemas."""

from __future__ import annotations

from datetime import datetime

import pytest

from cortex.libs.schemas.features import FeatureVector
from cortex.libs.schemas.intervention import (
    ErrorAnalysis,
    InterventionOutcome,
    InterventionPlan,
    UIPlan,
)
from cortex.libs.schemas.state import UserBaselines, UserState

# ---------------------------------------------------------------------------
# FeatureVector
# ---------------------------------------------------------------------------

class TestFeatureVector:
    def test_to_array_14_dimensions(self):
        """to_array() should return a 14-element list."""
        fv = FeatureVector(
            timestamp=1.0,
            hr=72.0,
            hrv_rmssd=50.0,
            hr_delta=1.0,
            blink_rate=17.0,
            blink_rate_delta=-1.0,
            shoulder_drop_ratio=0.3,
            forward_lean_angle=10.0,
            mouse_velocity_mean=400.0,
            mouse_velocity_variance=5000.0,
            click_frequency=0.5,
            keystroke_interval_variance=100.0,
            tab_switch_frequency=2.0,
            respiration_rate=15.0,
            thrashing_score=0.2,
        )
        arr = fv.to_array()
        assert len(arr) == 14

    def test_to_array_includes_respiration_rate(self):
        fv = FeatureVector(timestamp=1.0, respiration_rate=16.0, thrashing_score=0.3)
        arr = fv.to_array()
        assert 16.0 in arr

    def test_to_array_includes_thrashing_score(self):
        fv = FeatureVector(timestamp=1.0, thrashing_score=0.7)
        arr = fv.to_array()
        assert 0.7 in arr

    def test_has_respiration_true(self):
        fv = FeatureVector(timestamp=1.0, respiration_rate=14.0)
        assert fv.has_respiration is True

    def test_has_respiration_false(self):
        fv = FeatureVector(timestamp=1.0)
        assert fv.has_respiration is False

    def test_minimal_construction(self):
        """FeatureVector should construct with only timestamp."""
        fv = FeatureVector(timestamp=0.0)
        arr = fv.to_array()
        assert len(arr) == 14


# ---------------------------------------------------------------------------
# UserState and UserBaselines
# ---------------------------------------------------------------------------

class TestStateSchemas:
    def test_hypo_apnea_exists(self):
        """UserState.HYPO_APNEA must be a valid enum member."""
        assert UserState.HYPO_APNEA == "HYPO_APNEA"
        assert UserState.HYPO_APNEA.value == "HYPO_APNEA"

    def test_all_states(self):
        expected = {"FLOW", "HYPO", "HYPER", "RECOVERY", "HYPO_APNEA"}
        actual = {s.value for s in UserState}
        assert expected == actual

    def test_resp_baseline_default(self):
        baselines = UserBaselines()
        assert baselines.resp_baseline == 15.0

    def test_baselines_not_calibrated_by_default(self):
        baselines = UserBaselines()
        assert baselines.is_calibrated is False


# ---------------------------------------------------------------------------
# InterventionPlan — causal_explanation and consent_level
# ---------------------------------------------------------------------------

class TestInterventionPlan:
    @pytest.fixture
    def plan(self):
        return InterventionPlan(
            level="overlay_only",
            situation_summary="User is overwhelmed with 20 tabs",
            headline="Focus on one thing",
            primary_focus="Current task",
            micro_steps=["Close unrelated tabs"],
            ui_plan=UIPlan(),
            causal_explanation="HR elevated + 20 tabs + thrashing_score 0.8",
            consent_level="preview",
        )

    def test_causal_explanation_field(self, plan):
        assert "HR elevated" in plan.causal_explanation

    def test_consent_level_field(self, plan):
        assert plan.consent_level == "preview"

    def test_consent_level_values(self):
        for level in ("observe", "suggest", "preview", "reversible_act", "autonomous_act"):
            plan = InterventionPlan(
                level="overlay_only",
                situation_summary="summary",
                headline="headline",
                primary_focus="focus",
                micro_steps=["step"],
                ui_plan=UIPlan(),
                consent_level=level,
            )
            assert plan.consent_level == level


# ---------------------------------------------------------------------------
# ErrorAnalysis extended fields
# ---------------------------------------------------------------------------

class TestErrorAnalysis:
    def test_extended_fields_exist(self):
        ea = ErrorAnalysis(
            error_type="import",
            root_cause="Module not found",
            failing_abstraction="numpy.linalg",
            symbol_location="model.py:42",
            root_cause_category="missing_import",
            minimal_edit="pip install numpy",
        )
        assert ea.failing_abstraction == "numpy.linalg"
        assert ea.symbol_location == "model.py:42"
        assert ea.root_cause_category == "missing_import"
        assert ea.minimal_edit == "pip install numpy"

    def test_root_cause_category_literals(self):
        valid_categories = [
            "type_mismatch", "null_reference", "missing_import", "logic_error",
            "api_misuse", "concurrency", "config", "other",
        ]
        for cat in valid_categories:
            ea = ErrorAnalysis(
                error_type="test",
                root_cause="test",
                root_cause_category=cat,
            )
            assert ea.root_cause_category == cat

    def test_defaults(self):
        ea = ErrorAnalysis(error_type="syntax", root_cause="typo")
        assert ea.failing_abstraction == ""
        assert ea.symbol_location == ""
        assert ea.root_cause_category == "other"
        assert ea.minimal_edit == ""


# ---------------------------------------------------------------------------
# InterventionOutcome — helpfulness_score and user_rating
# ---------------------------------------------------------------------------

class TestInterventionOutcome:
    def test_helpfulness_score_field(self):
        outcome = InterventionOutcome(
            intervention_id="int_001",
            started_at=datetime.now(),
            user_action="engaged",
            helpfulness_score=0.75,
        )
        assert outcome.helpfulness_score == 0.75

    def test_user_rating_field(self):
        outcome = InterventionOutcome(
            intervention_id="int_002",
            started_at=datetime.now(),
            user_action="dismissed",
            user_rating="thumbs_down",
        )
        assert outcome.user_rating == "thumbs_down"

    def test_user_rating_none_by_default(self):
        outcome = InterventionOutcome(
            intervention_id="int_003",
            started_at=datetime.now(),
            user_action="engaged",
        )
        assert outcome.user_rating is None
        assert outcome.helpfulness_score is None

    def test_helpfulness_score_bounds(self):
        outcome = InterventionOutcome(
            intervention_id="int_004",
            started_at=datetime.now(),
            user_action="engaged",
            helpfulness_score=-1.0,
        )
        assert outcome.helpfulness_score == -1.0
