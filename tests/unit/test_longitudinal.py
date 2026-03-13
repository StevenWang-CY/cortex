"""Tests for the LongitudinalTracker (baseline drift / trend model)."""

from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import AsyncMock

import numpy as np
import pytest

from cortex.services.state_engine.longitudinal import LongitudinalTracker


class TestAccumulate:
    """Tests for LongitudinalTracker.accumulate (record_sample stores data)."""

    def test_accumulate_stores_hr_samples(self):
        tracker = LongitudinalTracker()
        tracker.accumulate(hr=72.0, hrv=50.0, resp=14.0)
        tracker.accumulate(hr=75.0, hrv=48.0, resp=15.0)
        assert len(tracker._hr_samples) == 2
        assert tracker._hr_samples == [72.0, 75.0]

    def test_accumulate_stores_hrv_samples(self):
        tracker = LongitudinalTracker()
        tracker.accumulate(hrv=55.0)
        tracker.accumulate(hrv=52.0)
        assert tracker._hrv_samples == [55.0, 52.0]

    def test_accumulate_stores_resp_samples(self):
        tracker = LongitudinalTracker()
        tracker.accumulate(resp=14.0)
        assert tracker._resp_samples == [14.0]

    def test_accumulate_ignores_none_values(self):
        tracker = LongitudinalTracker()
        tracker.accumulate(hr=None, hrv=None, resp=None)
        assert len(tracker._hr_samples) == 0
        assert len(tracker._hrv_samples) == 0
        assert len(tracker._resp_samples) == 0

    def test_accumulate_tracks_flow_seconds(self):
        tracker = LongitudinalTracker()
        tracker.accumulate(hr=70.0, state="FLOW", dt_seconds=0.5)
        tracker.accumulate(hr=70.0, state="FLOW", dt_seconds=0.5)
        assert tracker._flow_seconds == pytest.approx(1.0)

    def test_accumulate_tracks_hyper_seconds(self):
        tracker = LongitudinalTracker()
        tracker.accumulate(hr=90.0, state="HYPER", dt_seconds=0.5)
        tracker.accumulate(hr=90.0, state="HYPER", dt_seconds=0.5)
        tracker.accumulate(hr=90.0, state="HYPER", dt_seconds=0.5)
        assert tracker._hyper_seconds == pytest.approx(1.5)

    def test_accumulate_records_hourly_overload(self):
        tracker = LongitudinalTracker()
        tracker.accumulate(state="HYPER")
        tracker.accumulate(state="FLOW")
        # Should have entries in hourly_overload for the current hour
        total_entries = sum(len(v) for v in tracker._hourly_overload.values())
        assert total_entries == 2


class TestRecordIntervention:
    def test_record_intervention_accepted(self):
        tracker = LongitudinalTracker()
        tracker.record_intervention(accepted=True)
        assert tracker._intervention_count == 1
        assert tracker._intervention_accepted == 1

    def test_record_intervention_rejected(self):
        tracker = LongitudinalTracker()
        tracker.record_intervention(accepted=False)
        assert tracker._intervention_count == 1
        assert tracker._intervention_accepted == 0

    def test_record_multiple_interventions(self):
        tracker = LongitudinalTracker()
        tracker.record_intervention(accepted=True)
        tracker.record_intervention(accepted=False)
        tracker.record_intervention(accepted=True)
        assert tracker._intervention_count == 3
        assert tracker._intervention_accepted == 2


class TestSnapshotDaily:
    @pytest.mark.asyncio
    async def test_snapshot_returns_summary_dict(self):
        tracker = LongitudinalTracker()
        tracker.accumulate(hr=72.0, hrv=50.0, resp=14.0)
        tracker.accumulate(hr=74.0, hrv=48.0, resp=15.0)
        summary = await tracker.snapshot_daily()
        assert summary["date"] == date.today().isoformat()
        assert summary["hr_baseline"] == pytest.approx(73.0)
        assert summary["hrv_baseline"] == pytest.approx(49.0)
        assert summary["resp_baseline"] == pytest.approx(14.5)

    @pytest.mark.asyncio
    async def test_snapshot_defaults_when_no_samples(self):
        tracker = LongitudinalTracker()
        summary = await tracker.snapshot_daily()
        assert summary["hr_baseline"] == 72.0
        assert summary["hrv_baseline"] == 50.0
        assert summary["resp_baseline"] == 15.0


class TestComputeTrend:
    @pytest.mark.asyncio
    async def test_no_store_returns_stable(self):
        tracker = LongitudinalTracker(store=None)
        result = await tracker.compute_trend()
        assert result["trend"] == "stable"
        assert result["sensitivity_multiplier"] == 1.0

    @pytest.mark.asyncio
    async def test_declining_hrv_increases_sensitivity(self):
        """14 days of declining HRV should yield a 'declining' trend."""
        store = AsyncMock()

        # Simulate 14 days of declining HRV baselines
        baselines = {}
        from datetime import timedelta
        for i in range(14):
            d = date.today() - timedelta(days=i)
            key = f"daily_baseline:{d.isoformat()}"
            # HRV declines over time: older days have higher HRV, recent days lower
            # i=0 is today (lowest), i=13 is 13 days ago (highest)
            hrv_value = 32.0 + i * 2.0
            baselines[key] = {
                "date": d.isoformat(),
                "hrv_baseline": hrv_value,
                "hr_baseline": 72.0,
                "resp_baseline": 15.0,
            }

        async def mock_get_json(key):
            return baselines.get(key)

        store.get_json = mock_get_json

        tracker = LongitudinalTracker(store=store, window_days=30)
        result = await tracker.compute_trend()

        assert result["trend"] == "declining"
        # The slope should be negative for declining HRV
        assert result["hrv_slope_per_day"] < 0
        assert result["days_analyzed"] == 14

    @pytest.mark.asyncio
    async def test_sensitivity_multiplier_is_float_gte_half(self):
        """compute_sensitivity_multiplier returns float, clamped to >= 0.5."""
        store = AsyncMock()

        from datetime import timedelta
        baselines = {}
        for i in range(14):
            d = date.today() - timedelta(days=i)
            key = f"daily_baseline:{d.isoformat()}"
            # Steeply declining HRV
            hrv_value = 80.0 - i * 4.0
            baselines[key] = {
                "date": d.isoformat(),
                "hrv_baseline": hrv_value,
            }

        async def mock_get_json(key):
            return baselines.get(key)

        store.get_json = mock_get_json

        tracker = LongitudinalTracker(store=store, window_days=30)
        result = await tracker.compute_trend()

        multiplier = result["sensitivity_multiplier"]
        assert isinstance(multiplier, float)
        assert multiplier >= 0.5

    @pytest.mark.asyncio
    async def test_sensitivity_multiplier_property(self):
        """The sensitivity_multiplier property reflects the computed value."""
        tracker = LongitudinalTracker(store=None)
        assert isinstance(tracker.sensitivity_multiplier, float)
        assert tracker.sensitivity_multiplier == 1.0

        # After compute_trend with no store, stays 1.0
        await tracker.compute_trend()
        assert tracker.sensitivity_multiplier == 1.0

    @pytest.mark.asyncio
    async def test_insufficient_days_returns_stable(self):
        """Less than 7 days of data should return stable."""
        store = AsyncMock()

        from datetime import timedelta
        baselines = {}
        for i in range(5):  # Only 5 days — below _MIN_DAYS_FOR_TREND
            d = date.today() - timedelta(days=i)
            key = f"daily_baseline:{d.isoformat()}"
            baselines[key] = {
                "date": d.isoformat(),
                "hrv_baseline": 50.0 - i * 2.0,
            }

        async def mock_get_json(key):
            return baselines.get(key)

        store.get_json = mock_get_json

        tracker = LongitudinalTracker(store=store, window_days=30)
        result = await tracker.compute_trend()
        assert result["trend"] == "stable"
        assert result["sensitivity_multiplier"] == 1.0
