"""Tests for Handover — ShutdownDetector, HandoverSnapshot, MorningBriefing."""
import pytest
import tempfile
from pathlib import Path
from unittest.mock import patch
from datetime import datetime

from cortex.services.handover.detector import ShutdownDetector
from cortex.services.handover.snapshot import HandoverSnapshot
from cortex.services.handover.briefing import MorningBriefing, BriefingContent


class TestShutdownDetector:
    def test_no_trigger_during_day(self):
        """Before late hour → no trigger."""
        detector = ShutdownDetector(late_hour=22)
        with patch("cortex.services.handover.detector.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 13, 14, 0)
            result = detector.should_handover(
                posture_slump=0.8, hrv=30.0, error_count=5, current_time=0.0,
            )
            assert result is False

    def test_trigger_late_with_fatigue_signals(self):
        """Late hour + 2/3 signals sustained → trigger."""
        detector = ShutdownDetector(hrv_baseline=50.0, late_hour=22, cooldown=0.0)
        with patch("cortex.services.handover.detector.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 13, 23, 0)
            # First call starts accumulation
            detector.should_handover(posture_slump=0.8, hrv=30.0, error_count=5, current_time=0.0)
            # After min_duration (300s)
            result = detector.should_handover(
                posture_slump=0.8, hrv=30.0, error_count=5, current_time=400.0,
            )
            assert result is True

    def test_needs_two_of_three_signals(self):
        """Only 1 signal → no trigger."""
        detector = ShutdownDetector(hrv_baseline=50.0, late_hour=22)
        with patch("cortex.services.handover.detector.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 13, 23, 0)
            result = detector.should_handover(
                posture_slump=0.8, hrv=50.0, error_count=0, current_time=0.0,
            )
            assert result is False

    def test_default_late_hour_from_config(self):
        """Default late_hour should be 23 from HandoverConfig."""
        from cortex.libs.config.settings import HandoverConfig
        detector = ShutdownDetector()
        assert detector._late_hour == 23

    def test_late_hour_from_config_object(self):
        """late_hour should be read from HandoverConfig when provided."""
        from cortex.libs.config.settings import HandoverConfig
        cfg = HandoverConfig(late_hour=23)
        detector = ShutdownDetector(config=cfg)
        assert detector._late_hour == 23

    def test_late_hour_explicit_overrides_config(self):
        """Explicit late_hour parameter should override config."""
        from cortex.libs.config.settings import HandoverConfig
        cfg = HandoverConfig(late_hour=23)
        detector = ShutdownDetector(late_hour=21, config=cfg)
        assert detector._late_hour == 21

    def test_cooldown(self):
        detector = ShutdownDetector(hrv_baseline=50.0, late_hour=22, cooldown=3600.0)
        with patch("cortex.services.handover.detector.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 13, 23, 0)
            detector.should_handover(posture_slump=0.8, hrv=30.0, error_count=5, current_time=0.0)
            detector.should_handover(posture_slump=0.8, hrv=30.0, error_count=5, current_time=400.0)
            # Should be in cooldown now
            result = detector.should_handover(
                posture_slump=0.8, hrv=30.0, error_count=5, current_time=500.0,
            )
            assert result is False


class TestHandoverSnapshot:
    @pytest.mark.asyncio
    async def test_capture_and_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = HandoverSnapshot(storage_path=tmp)
            path = await snapshot.capture_and_write(
                editor_context={"file_path": "main.py", "visible_range": [1, 50]},
                terminal_context={"last_n_lines": ["$ pytest", "PASSED"]},
            )
            assert path.exists()
            content = path.read_text()
            assert "main.py" in content
            assert "Handover Brief" in content

    def test_get_latest_handover_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = HandoverSnapshot(storage_path=tmp)
            assert snapshot.get_latest_handover() is None


class TestMorningBriefing:
    @pytest.mark.asyncio
    async def test_no_handover_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            briefing = MorningBriefing(storage_path=tmp)
            result = await briefing.check_and_generate()
            assert result is None

    @pytest.mark.asyncio
    async def test_generates_from_existing_handover(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Create a fake handover file
            handovers_dir = Path(tmp) / "handovers"
            handovers_dir.mkdir()
            handover_file = handovers_dir / "2026-03-12.md"
            handover_file.write_text(
                "# Handover Brief — 2026-03-12 at 23:00\n\n"
                "> Auto-generated\n\n"
                "## Summary\n\nWorking on auth module.\n\n"
                "## TODO\n- [ ] Fix login bug\n- [ ] Add tests\n"
            )
            briefing = MorningBriefing(storage_path=tmp)
            result = await briefing.check_and_generate()
            assert result is not None
            assert isinstance(result, BriefingContent)
            assert "auth" in result.summary.lower() or "Working" in result.summary
            assert len(result.action_items) >= 2

    def test_to_ws_payload(self):
        briefing_mgr = MorningBriefing()
        content = BriefingContent(
            title="Test", summary="Summary", action_items=["item1"],
            handover_path="/tmp/test.md", raw_markdown="# Test",
        )
        payload = briefing_mgr.to_ws_payload(content)
        assert payload["title"] == "Test"
        assert "action_items" in payload
