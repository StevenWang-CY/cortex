"""P2-20: ShutdownDetector._error_timestamps is a bounded deque (maxlen=1024).

Ensures:
1. Feeding 5000 timestamps keeps the deque at most 1024 entries.
2. The 5-minute error-rate window behaviour is unchanged for a
   representative scenario (3 recent errors is above the threshold=3
   after 5 recent errors).
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from unittest.mock import patch

from cortex.services.handover.detector import ShutdownDetector


class TestHandoverDetectorBounded:
    def test_deque_bounded_at_1024(self) -> None:
        """5000 recorded errors must never exceed maxlen=1024."""
        detector = ShutdownDetector()
        now = 1_000_000.0
        for i in range(5000):
            detector.record_error(timestamp=now + i * 0.001)

        assert isinstance(detector._error_timestamps, deque)
        assert len(detector._error_timestamps) <= 1024

    def test_stale_entries_pruned(self) -> None:
        """Entries older than 5 minutes are pruned."""
        detector = ShutdownDetector()
        base = 1_000_000.0

        # Add 10 old errors (> 5 minutes ago)
        for i in range(10):
            detector.record_error(timestamp=base - 400.0 + i)

        # Add 5 recent errors
        for i in range(5):
            detector.record_error(timestamp=base + i)

        # After the last record_error, stale ones should be pruned
        assert len(detector._error_timestamps) == 5, (
            f"expected 5 recent entries, got {len(detector._error_timestamps)}"
        )

    def test_error_count_in_window_correct(self) -> None:
        """should_handover uses the internal error count from the deque correctly."""
        detector = ShutdownDetector(
            hrv_baseline=50.0,
            late_hour=22,
            cooldown=0.0,
            error_rate_threshold=3,
        )
        now = 1_000_000.0

        # Record 5 recent errors (> threshold of 3)
        for i in range(5):
            detector.record_error(timestamp=now - 10.0 + i)

        with patch("cortex.services.handover.detector.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 13, 23, 0)
            # Start fatigue accumulation
            detector.should_handover(
                posture_slump=0.8, hrv=30.0, current_time=now
            )
            # After sustained period (> MIN_DURATION_SECONDS=300)
            result = detector.should_handover(
                posture_slump=0.8, hrv=30.0, current_time=now + 400.0
            )

        assert result is True, "Expected handover trigger with sufficient error count"

    def test_behavior_unchanged_for_representative_window(self) -> None:
        """Bounded deque does not break the 5-min error-rate calculation."""
        detector = ShutdownDetector(error_rate_threshold=3)
        now = 1_000_000.0

        # Add 4 errors within 5-minute window → above threshold
        for i in range(4):
            detector.record_error(timestamp=now - 60.0 + i)

        error_count = len(detector._error_timestamps)
        assert error_count == 4
