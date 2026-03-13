"""Tests for the handover subsystem: ShutdownDetector, HandoverSnapshot, MorningBriefing."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from cortex.services.handover.briefing import MorningBriefing
from cortex.services.handover.detector import ShutdownDetector


class TestShutdownDetectorTrigger:
    """ShutdownDetector should fire when compound fatigue signals are met."""

    def _make_detector(self, hrv_baseline: float = 50.0) -> ShutdownDetector:
        return ShutdownDetector(hrv_baseline=hrv_baseline, cooldown=0.0)

    def test_all_signals_plus_late_hour_triggers_handover(self):
        """posture_slump > 0.6, HRV dropping, errors rising, late hour -> should_handover."""
        detector = self._make_detector(hrv_baseline=50.0)

        # Mock datetime.now() to return a late hour (23:00)
        fake_now = datetime(2026, 3, 13, 23, 0, 0)
        t = 1000.0

        with patch("cortex.services.handover.detector.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now

            # First call starts accumulating (all signals met but duration not yet)
            result1 = detector.should_handover(
                posture_slump=0.7,
                hrv=30.0,       # 30/50 = 0.6 < 0.7 threshold
                error_count=5,  # >= 3
                current_time=t,
            )
            assert result1 is False  # Duration not met yet
            assert detector.is_accumulating

            # Second call after 5+ minutes should trigger
            result2 = detector.should_handover(
                posture_slump=0.7,
                hrv=30.0,
                error_count=5,
                current_time=t + 301.0,  # > 300s
            )
            assert result2 is True

    def test_two_of_three_signals_sufficient(self):
        """Only 2 of 3 physiological signals needed (plus late hour)."""
        detector = self._make_detector(hrv_baseline=50.0)
        fake_now = datetime(2026, 3, 13, 23, 30, 0)
        t = 2000.0

        with patch("cortex.services.handover.detector.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now

            # Posture + errors but no HRV drop
            detector.should_handover(
                posture_slump=0.8,
                hrv=45.0,       # 45/50 = 0.9, NOT dropping
                error_count=5,
                current_time=t,
            )
            result = detector.should_handover(
                posture_slump=0.8,
                hrv=45.0,
                error_count=5,
                current_time=t + 301.0,
            )
            assert result is True


class TestShutdownDetectorNoTrigger:
    """ShutdownDetector should NOT trigger under normal conditions."""

    def _make_detector(self) -> ShutdownDetector:
        return ShutdownDetector(hrv_baseline=50.0, cooldown=0.0)

    def test_normal_conditions_no_trigger(self):
        """Good posture, normal HRV, no errors, reasonable hour -> no handover."""
        detector = self._make_detector()
        fake_now = datetime(2026, 3, 13, 14, 0, 0)  # 2 PM

        with patch("cortex.services.handover.detector.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            result = detector.should_handover(
                posture_slump=0.2,
                hrv=48.0,
                error_count=0,
                current_time=1000.0,
            )
            assert result is False
            assert not detector.is_accumulating

    def test_not_late_hour_no_trigger(self):
        """Even with all physiological signals, daytime blocks trigger."""
        detector = self._make_detector()
        fake_now = datetime(2026, 3, 13, 15, 0, 0)  # 3 PM

        with patch("cortex.services.handover.detector.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            result = detector.should_handover(
                posture_slump=0.9,
                hrv=20.0,
                error_count=10,
                current_time=1000.0,
            )
            assert result is False

    def test_only_one_signal_no_trigger(self):
        """Only 1 of 3 signals is not enough."""
        detector = self._make_detector()
        fake_now = datetime(2026, 3, 13, 23, 0, 0)

        with patch("cortex.services.handover.detector.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            # Only posture is bad
            result = detector.should_handover(
                posture_slump=0.8,
                hrv=48.0,       # normal
                error_count=0,  # no errors
                current_time=1000.0,
            )
            assert result is False
            assert not detector.is_accumulating

    def test_cooldown_prevents_repeated_triggers(self):
        """After triggering, cooldown prevents immediate re-trigger."""
        detector = ShutdownDetector(hrv_baseline=50.0, cooldown=3600.0)
        fake_now = datetime(2026, 3, 13, 23, 0, 0)
        t = 5000.0

        with patch("cortex.services.handover.detector.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now

            # First: accumulate
            detector.should_handover(posture_slump=0.8, hrv=25.0, error_count=5, current_time=t)
            # Trigger
            detector.should_handover(posture_slump=0.8, hrv=25.0, error_count=5, current_time=t + 301.0)

            # Immediately after, cooldown should block
            result = detector.should_handover(
                posture_slump=0.8, hrv=25.0, error_count=5, current_time=t + 400.0,
            )
            assert result is False


class TestMorningBriefing:
    """MorningBriefing should return None when no handover file exists."""

    @pytest.mark.asyncio
    async def test_check_and_generate_returns_none_when_no_file(self, tmp_path: Path):
        """No handover files -> check_and_generate returns None."""
        storage = tmp_path / "storage"
        storage.mkdir()
        # Create the handovers dir but leave it empty
        (storage / "handovers").mkdir()

        briefing = MorningBriefing(storage_path=str(storage))
        result = await briefing.check_and_generate()
        assert result is None

    @pytest.mark.asyncio
    async def test_check_and_generate_returns_briefing_when_file_exists(self, tmp_path: Path):
        """When a handover file exists, should return BriefingContent."""
        storage = tmp_path / "storage"
        handovers = storage / "handovers"
        handovers.mkdir(parents=True)

        # Write a handover file
        content = (
            "# Handover Brief -- 2026-03-12 at 23:30\n"
            "> Auto-generated.\n"
            "## Summary\n"
            "You were working on the login module.\n"
            "## TODO\n"
            "- [ ] Fix the auth bug\n"
            "- [ ] Write tests\n"
        )
        (handovers / "2026-03-12.md").write_text(content)

        briefing = MorningBriefing(storage_path=str(storage))
        result = await briefing.check_and_generate()

        assert result is not None
        assert "login module" in result.summary
        assert len(result.action_items) >= 2
        assert "Fix the auth bug" in result.action_items[0]
