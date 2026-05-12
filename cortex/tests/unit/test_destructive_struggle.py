"""Unit tests for DestructiveStruggleDetector."""


from cortex.services.state_engine.destructive_struggle import (
    DestructiveStruggleDetector,
)

base_t = 1000.0


class TestDestructiveStruggleDetector:
    """Tests for the DestructiveStruggleDetector."""

    def test_comprehension_pathway_triggers(self):
        """Comprehension triggers: reread>2, load rising, stage dwell>300s."""
        detector = DestructiveStruggleDetector()
        estimate = detector.update(
            reread_count=4,
            wrong_answer_count=0,
            code_delete_ratio=0.0,
            stage_dwell_s=400.0,
            allostatic_load=0.8,
            allostatic_load_prev=0.5,
            hrv_rmssd=60.0,
            hrv_baseline=60.0,
            wa_timestamps=[],
            current_time=base_t,
        )
        assert estimate.is_destructive is True
        assert estimate.pathway == "comprehension"
        assert estimate.confidence > 0.0

    def test_comprehension_pathway_does_not_trigger_low_reread(self):
        """Comprehension does NOT trigger when reread<=2."""
        detector = DestructiveStruggleDetector()
        estimate = detector.update(
            reread_count=2,
            wrong_answer_count=0,
            code_delete_ratio=0.0,
            stage_dwell_s=400.0,
            allostatic_load=0.8,
            allostatic_load_prev=0.5,
            hrv_rmssd=60.0,
            hrv_baseline=60.0,
            wa_timestamps=[],
            current_time=base_t,
        )
        assert estimate.is_destructive is False

    def test_implementation_pathway_triggers(self):
        """Implementation triggers: WA>2 in 10min, delete ratio>0.5, HRV<80% baseline."""
        detector = DestructiveStruggleDetector()
        # 3 WAs within the 10-minute window
        wa_times = [base_t - 300.0, base_t - 200.0, base_t - 100.0]
        estimate = detector.update(
            reread_count=0,
            wrong_answer_count=3,
            code_delete_ratio=0.7,
            stage_dwell_s=0.0,
            allostatic_load=0.5,
            allostatic_load_prev=0.5,
            hrv_rmssd=40.0,      # below 80% of baseline (48.0)
            hrv_baseline=60.0,
            wa_timestamps=wa_times,
            current_time=base_t,
        )
        assert estimate.is_destructive is True
        assert estimate.pathway == "implementation"
        assert estimate.confidence > 0.0

    def test_implementation_pathway_does_not_trigger_low_delete_ratio(self):
        """Implementation does NOT trigger when delete ratio<=0.5."""
        detector = DestructiveStruggleDetector()
        wa_times = [base_t - 300.0, base_t - 200.0, base_t - 100.0]
        estimate = detector.update(
            reread_count=0,
            wrong_answer_count=3,
            code_delete_ratio=0.3,  # below threshold
            stage_dwell_s=0.0,
            allostatic_load=0.5,
            allostatic_load_prev=0.5,
            hrv_rmssd=40.0,
            hrv_baseline=60.0,
            wa_timestamps=wa_times,
            current_time=base_t,
        )
        assert estimate.is_destructive is False

    def test_neither_pathway_is_not_destructive(self):
        """Neither pathway triggered means is_destructive=False."""
        detector = DestructiveStruggleDetector()
        estimate = detector.update(
            reread_count=0,
            wrong_answer_count=0,
            code_delete_ratio=0.0,
            stage_dwell_s=10.0,
            allostatic_load=0.3,
            allostatic_load_prev=0.3,
            hrv_rmssd=60.0,
            hrv_baseline=60.0,
            wa_timestamps=[],
            current_time=base_t,
        )
        assert estimate.is_destructive is False
        assert estimate.pathway == ""
        assert estimate.confidence == 0.0

    def test_reset_clears_state(self):
        """reset() returns state to default (not destructive)."""
        detector = DestructiveStruggleDetector()
        detector.update(
            reread_count=4,
            wrong_answer_count=0,
            code_delete_ratio=0.0,
            stage_dwell_s=400.0,
            allostatic_load=0.8,
            allostatic_load_prev=0.5,
            hrv_rmssd=60.0,
            hrv_baseline=60.0,
            wa_timestamps=[],
            current_time=base_t,
        )
        assert detector._latest.is_destructive is True

        detector.reset()

        assert detector._latest.is_destructive is False
        assert detector._latest.pathway == ""
        assert detector._latest.confidence == 0.0
