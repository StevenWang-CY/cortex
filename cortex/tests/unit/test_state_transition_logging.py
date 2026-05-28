"""P2-16: ScoreSmoother emits a structured STATE_TRANSITION structlog event.

When a state transition is confirmed (after dwell time), the smoother
must emit a ``state_transition`` event via structlog carrying:
  - from_state
  - to_state
  - dwell_seconds
  - confidence
  - correlation_id (may be None when no context var is set)

The test captures the structlog events by patching
``cortex.services.state_engine.smoother.log_state_transition``.
"""

from __future__ import annotations

from unittest.mock import patch

from cortex.libs.config.settings import StateConfig
from cortex.libs.schemas.state import SignalQuality, StateScores, UserState
from cortex.services.state_engine.smoother import ScoreSmoother


def _sq(physio: float = 0.8) -> SignalQuality:
    return SignalQuality(physio=physio, kinematics=0.7, telemetry=0.8)


def _scores(hyper: float = 0.0, flow: float = 0.9, hypo: float = 0.0, recovery: float = 0.0) -> StateScores:
    return StateScores(flow=flow, hypo=hypo, hyper=hyper, recovery=recovery)


class TestStateTransitionLogging:
    def test_transition_emits_structlog_event(self) -> None:
        """FLOW → HYPER transition must fire log_state_transition with required fields.

        The EMA smoother requires multiple updates before the smoothed score
        crosses the entry/exit hysteresis thresholds, so we run enough
        updates to ensure the transition fires.
        """
        cfg = StateConfig(
            entry_threshold=0.5,
            exit_threshold=0.3,
            hyper_dwell_seconds=0.0,  # no dwell required for test
            hypo_dwell_seconds=0.0,
            flow_dwell_seconds=0.0,
        )
        smoother = ScoreSmoother(config=cfg)

        with patch(
            "cortex.services.state_engine.smoother.log_state_transition"
        ) as mock_log:
            # Establish FLOW state with 20 updates
            for i in range(20):
                smoother.update(_scores(flow=0.9, hyper=0.1), _sq(), timestamp=float(i))

            # Drive toward HYPER with enough updates to cross both thresholds
            for i in range(20):
                smoother.update(
                    _scores(flow=0.1, hyper=0.9), _sq(), timestamp=float(20 + i)
                )

        # A STATE_TRANSITION event should have been logged at some point
        assert mock_log.called, "log_state_transition was never called during transition"

        # Inspect the most recent call
        last_call = mock_log.call_args
        kwargs = last_call.kwargs if last_call.kwargs else {}
        args = last_call.args if last_call.args else ()

        # The helper signature: (from_state, to_state, confidence, reasons, dwell_seconds, *, correlation_id)
        # Positional args
        if len(args) >= 5:
            from_state, to_state, confidence, reasons, dwell_seconds = args[:5]
            correlation_id = kwargs.get("correlation_id")  # noqa: F841 — only kwargs membership is asserted below
        else:
            from_state = kwargs.get("from_state") or args[0]
            to_state = kwargs.get("to_state") or args[1]
            confidence = kwargs.get("confidence") or args[2]
            dwell_seconds = kwargs.get("dwell_seconds") or args[4] if len(args) > 4 else kwargs.get("dwell_seconds")
            correlation_id = kwargs.get("correlation_id")  # noqa: F841 — only kwargs membership is asserted below

        assert from_state == UserState.FLOW.value, f"expected from_state=FLOW, got {from_state!r}"
        assert to_state == UserState.HYPER.value, f"expected to_state=HYPER, got {to_state!r}"
        assert isinstance(confidence, float), f"confidence must be float, got {type(confidence)}"
        assert isinstance(dwell_seconds, float), f"dwell_seconds must be float, got {type(dwell_seconds)}"
        # correlation_id may be None (no cid bound in test) — but the keyword must have been passed
        assert "correlation_id" in (last_call.kwargs or {}), (
            "correlation_id keyword argument must be passed to log_state_transition"
        )

    def test_no_transition_no_log_call(self) -> None:
        """Staying in the same state must NOT emit a STATE_TRANSITION event."""
        cfg = StateConfig(entry_threshold=0.5, exit_threshold=0.3)
        smoother = ScoreSmoother(config=cfg)

        with patch(
            "cortex.services.state_engine.smoother.log_state_transition"
        ) as mock_log:
            # 30 updates all in stable FLOW territory — no transition should fire
            for ts in range(30):
                smoother.update(_scores(flow=0.9, hyper=0.1), _sq(), timestamp=float(ts))

        assert not mock_log.called, "log_state_transition should not fire when state is stable"
