"""P2-21: SessionReportGenerator preserves raw_dt_seconds on clock anomaly.

When a negative dt is detected in ``record_state``, the clock anomaly
record must carry the original (un-clamped) ``raw_dt_seconds`` value.
The state duration itself must be clamped to 0 (no negative contribution).
"""

from __future__ import annotations

import pytest

from cortex.services.session_report.generator import SessionReportGenerator


class TestClockAnomalyPreservesRaw:
    def test_negative_dt_records_raw_dt_seconds(self) -> None:
        """Injecting a -5.0s dt must produce a clock anomaly with raw_dt_seconds == -5.0."""
        gen = SessionReportGenerator()
        gen.start()

        base = 1_000_000.0
        gen.record_state("FLOW", base)

        # Inject a backward jump: new timestamp < old start
        backward_ts = base - 5.0
        gen.record_state("HYPER", backward_ts)

        anomalies = gen.clock_anomalies
        assert len(anomalies) == 1, f"expected 1 anomaly, got {len(anomalies)}"

        anomaly = anomalies[0]
        assert "raw_dt_seconds" in anomaly, (
            f"clock anomaly dict missing 'raw_dt_seconds' key: {anomaly!r}"
        )
        assert anomaly["raw_dt_seconds"] == pytest.approx(-5.0), (
            f"expected raw_dt_seconds=-5.0, got {anomaly['raw_dt_seconds']!r}"
        )
        assert anomaly["kind"] == "ntp_backjump"

    def test_clamped_state_duration_is_zero(self) -> None:
        """Negative dt must not contribute to accumulated state duration."""
        gen = SessionReportGenerator()
        gen.start()

        base = 1_000_000.0
        gen.record_state("FLOW", base)
        gen.record_state("HYPER", base - 5.0)  # backward jump

        # The FLOW bucket must have received 0 seconds (clamped, not -5)
        assert gen._state_durations["FLOW"] == pytest.approx(0.0), (
            f"expected FLOW duration=0.0 after clamp, got {gen._state_durations['FLOW']}"
        )

    def test_normal_dt_has_no_anomaly(self) -> None:
        """A forward-moving dt must not produce any clock anomaly."""
        gen = SessionReportGenerator()
        gen.start()

        base = 1_000_000.0
        gen.record_state("FLOW", base)
        gen.record_state("HYPER", base + 60.0)

        assert len(gen.clock_anomalies) == 0

    def test_raw_dt_matches_actual_negative_value(self) -> None:
        """raw_dt_seconds must exactly equal the un-clamped dt value."""
        gen = SessionReportGenerator()
        gen.start()

        base = 1_000_000.0
        gen.record_state("FLOW", base)
        gen.record_state("HYPER", base - 12.3)

        anomaly = gen.clock_anomalies[0]
        assert anomaly["raw_dt_seconds"] == pytest.approx(-12.3, rel=1e-6)
