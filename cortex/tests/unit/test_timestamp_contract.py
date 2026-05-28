"""P1-12: Timestamp contract tests — all float timestamps must be wall-clock.

Verifies:
- StateEstimate.timestamp is epoch-seconds (much larger than monotonic).
- FeatureVector.timestamp is epoch-seconds.
- FrameMeta.timestamp is epoch-seconds.
- StateTransition.timestamp is epoch-seconds.
- Values are within 60 seconds of time.time() when set via time.time().
"""

from __future__ import annotations

import time

from cortex.libs.schemas.features import FeatureVector, FrameMeta
from cortex.libs.schemas.state import (
    SignalQuality,
    StateEstimate,
    StateScores,
    StateTransition,
    UserState,
)


def _make_state_estimate(ts: float) -> StateEstimate:
    return StateEstimate(
        state=UserState.FLOW,
        confidence=0.8,
        scores=StateScores(flow=0.8, hypo=0.1, hyper=0.0, recovery=0.1),
        signal_quality=SignalQuality(physio=0.7, kinematics=0.6, telemetry=0.8),
        timestamp=ts,
    )


class TestTimestampIsWallClock:
    def test_state_estimate_timestamp_is_epoch(self) -> None:
        """timestamp set from time.time() must be epoch-seconds (>> monotonic)."""
        now = time.time()
        est = _make_state_estimate(now)
        # Epoch seconds are ~1.7 billion; monotonic seconds are tiny.
        assert abs(est.timestamp) > time.monotonic() * 1000, (
            "timestamp looks like monotonic, not epoch"
        )

    def test_state_estimate_timestamp_within_60s_of_now(self) -> None:
        before = time.time()
        est = _make_state_estimate(time.time())
        after = time.time()
        assert before - 1 <= est.timestamp <= after + 1

    def test_feature_vector_timestamp_is_epoch(self) -> None:
        now = time.time()
        fv = FeatureVector(timestamp=now)
        assert abs(fv.timestamp) > time.monotonic() * 1000

    def test_frame_meta_timestamp_is_epoch(self) -> None:
        now = time.time()
        fm = FrameMeta(
            timestamp=now,
            face_detected=True,
            face_confidence=0.9,
            brightness_score=0.8,
            blur_score=0.9,
            motion_score=0.1,
        )
        assert abs(fm.timestamp) > time.monotonic() * 1000

    def test_state_transition_timestamp_is_epoch(self) -> None:
        now = time.time()
        tr = StateTransition(
            timestamp=now,
            from_state="FLOW",
            to_state="HYPER",
            from_confidence=0.7,
            to_confidence=0.85,
            dwell_seconds=30.0,
        )
        assert abs(tr.timestamp) > time.monotonic() * 1000

    def test_epoch_sanity_check_gt_monotonic(self) -> None:
        """Epoch seconds (circa 1.7e9) must be much larger than monotonic.

        Epoch time is ~1.7 billion seconds since 1970.  Even a machine
        that has been running for a full year (~31 million seconds) yields
        epoch >> monotonic.  We assert epoch > 1e9 as a baseline check
        (the year 2001 is more than enough headroom)."""
        now = time.time()
        # epoch seconds must be > 1 billion (any date after ~year 2001)
        assert now > 1e9, f"time.time() returned {now}, expected > 1e9 (epoch)"
        # and epoch seconds are definitely larger than monotonic
        assert now > time.monotonic(), (
            "time.time() must be greater than time.monotonic() for any sane system"
        )
