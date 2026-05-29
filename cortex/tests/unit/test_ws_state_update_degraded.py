"""Audit Wave-2 follow-up: STATE_UPDATE WS payload carries F18 envelope.

The F18 fix (audit/findings.md) added ``source`` and ``degraded`` to the
HTTP ``/state/infer`` response envelope, and the dashboard's advanced tab
reads ``payload.get("degraded")`` / ``payload.get("source")`` to toggle
its "classifier unavailable" banner.

The drift: the dashboard banner is fed by the WS ``STATE_UPDATE``
broadcast, not by ``/state/infer``. ``WebSocketServer._make_state_update``
never emitted ``source`` or ``degraded``, so the banner could not fire
through the WS path — F18's intent was silently broken end-to-end.

Worse, the dashboard's secondary check
``payload.get("source") not in (None, "classifier")`` conflated the
``StateInferResponse.source`` literal (``classifier``/``fallback``) with
``StateEstimate.classifier_source`` (``rule``/``ml``/``ensemble``). The
two fields are unrelated; on a healthy ``"rule"`` estimate the banner
would have flipped True and stuck visible.

This test pins the contract: ``_make_state_update`` MUST stamp
``source`` and ``degraded`` on the WS payload so the dashboard's
existing reader closes the F18 loop without further plumbing.
"""

from __future__ import annotations

from cortex.libs.schemas.state import SignalQuality, StateEstimate, StateScores
from cortex.services.api_gateway.websocket_server import WebSocketServer


def _classifier_estimate() -> StateEstimate:
    """A healthy estimate produced by the rule scorer + smoother."""
    return StateEstimate(
        state="FLOW",
        confidence=0.82,
        scores=StateScores(flow=0.82, hypo=0.05, hyper=0.08, recovery=0.05),
        reasons=["physio steady", "no thrashing"],
        signal_quality=SignalQuality(
            physio=0.9, kinematics=0.8, telemetry=0.7,
        ),
        timestamp=100.0,
        dwell_seconds=12.5,
        classifier_source="rule",
    )


def _fallback_estimate() -> StateEstimate:
    """A synthetic estimate produced when the engines were unavailable —
    mirrors what ``routes.py`` constructs in the F18 fallback branch."""
    return StateEstimate(
        state="FLOW",
        confidence=0.5,
        scores=StateScores(flow=0.5, hypo=0.0, hyper=0.0, recovery=0.0),
        reasons=["No state engine registered, using default"],
        signal_quality=SignalQuality(
            physio=0.0, kinematics=0.0, telemetry=0.0,
        ),
        timestamp=100.0,
        dwell_seconds=0.0,
        # classifier_source intentionally None — the fallback path
        # cannot name a real engine.
    )


def _low_signal_estimate() -> StateEstimate:
    """A REAL classifier estimate (``classifier_source`` populated) but
    produced on a signal whose fused quality fell below the
    acceptability floor (``SignalQuality.overall < 0.3``).

    This is the production case finding #3 flagged: the smoother always
    stamps a ``classifier_source`` ("rule"/"ml"/"ensemble") so the old
    ``classifier_source is None`` condition NEVER fired here, and the
    dashboard's degraded banner could not surface a genuinely poor
    signal. overall = 0.4*0.1 + 0.3*0.1 + 0.3*0.1 = 0.10 < 0.3.
    """
    return StateEstimate(
        state="HYPER",
        confidence=0.61,
        scores=StateScores(flow=0.1, hypo=0.1, hyper=0.61, recovery=0.0),
        reasons=["thrashing"],
        signal_quality=SignalQuality(
            physio=0.1, kinematics=0.1, telemetry=0.1,
        ),
        timestamp=100.0,
        dwell_seconds=4.0,
        classifier_source="rule",
    )


def test_state_update_classifier_payload_marks_source_classifier() -> None:
    """A normal scorer-derived estimate produces ``source=classifier`` and
    ``degraded=False`` on the WS frame so the dashboard banner stays
    hidden during healthy operation."""
    server = WebSocketServer()
    msg = server._make_state_update(_classifier_estimate())
    assert msg.payload["source"] == "classifier"
    assert msg.payload["degraded"] is False


def test_state_update_fallback_payload_marks_source_fallback() -> None:
    """An estimate without a ``classifier_source`` (the route's synthetic
    fallback) produces ``source=fallback`` and ``degraded=True`` so the
    dashboard banner flips on across the WS path."""
    server = WebSocketServer()
    msg = server._make_state_update(_fallback_estimate())
    assert msg.payload["source"] == "fallback"
    assert msg.payload["degraded"] is True


def test_state_update_low_signal_marks_degraded_despite_classifier() -> None:
    """Finding #3: a real classifier estimate (``classifier_source`` set)
    whose fused signal quality fell below the acceptability floor MUST
    still broadcast ``degraded=True`` / ``source=fallback`` so the
    dashboard banner fires. The old ``classifier_source is None`` test
    never caught this because the live smoother always names a source."""
    server = WebSocketServer()
    msg = server._make_state_update(_low_signal_estimate())
    assert msg.payload["degraded"] is True
    assert msg.payload["source"] == "fallback"
    # The debug-overlay field is preserved — the real engine is still
    # named even though the envelope is flagged degraded.
    assert msg.payload["classifier_source"] == "rule"


def test_state_update_payload_keeps_classifier_source_field() -> None:
    """The debug-overlay-facing ``classifier_source`` field must keep its
    existing semantics (``rule``/``ml``/``ensemble``/``None``). The new
    ``source`` field is orthogonal — both can be present on the same
    frame without conflict."""
    server = WebSocketServer()
    msg = server._make_state_update(_classifier_estimate())
    # Debug fields preserved.
    assert msg.payload["classifier_source"] == "rule"
    # New envelope fields landed alongside, not in place of, the debug ones.
    assert "source" in msg.payload
    assert "degraded" in msg.payload
