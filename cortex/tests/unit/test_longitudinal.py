"""Tests for LongitudinalTracker — multi-day baseline drift detection."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from cortex.services.state_engine.longitudinal import LongitudinalTracker


class TestLongitudinalTracker:
    def setup_method(self):
        self.mock_store = MagicMock()
        self.mock_store.get_json = AsyncMock(return_value=None)
        self.mock_store.set_json = AsyncMock()
        self.tracker = LongitudinalTracker(store=self.mock_store)

    def test_accumulate_hr(self):
        self.tracker.accumulate(hr=72.0)
        assert len(self.tracker._hr_samples) == 1

    def test_accumulate_none_skipped(self):
        self.tracker.accumulate(hr=None, hrv=None)
        assert len(self.tracker._hr_samples) == 0
        assert len(self.tracker._hrv_samples) == 0

    def test_accumulate_tracks_state_duration(self):
        self.tracker.accumulate(state="FLOW", dt_seconds=1.0)
        assert self.tracker._flow_seconds == 1.0
        self.tracker.accumulate(state="HYPER", dt_seconds=2.0)
        assert self.tracker._hyper_seconds == 2.0

    def test_sensitivity_multiplier_default(self):
        assert self.tracker.sensitivity_multiplier == 1.0

    @pytest.mark.asyncio
    async def test_snapshot_daily(self):
        self.tracker.accumulate(hr=70.0, hrv=45.0, resp=15.0)
        await self.tracker.snapshot_daily()
        # Should have called set_json on the store
        self.mock_store.set_json.assert_called()

    @pytest.mark.asyncio
    async def test_compute_trend_no_data(self):
        self.mock_store.get_json = AsyncMock(return_value=None)
        trend = await self.tracker.compute_trend()
        assert isinstance(trend, dict)
        assert "sensitivity_multiplier" in trend
