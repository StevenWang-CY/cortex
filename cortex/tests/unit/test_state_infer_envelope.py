"""Audit F18 — ``/state/infer`` envelope distinguishes classifier from fallback.

Pre-fix, the fallback path returned a synthetic ``confidence=0.5`` that
was structurally identical to a real rule-scorer confidence of 0.5. The
client could not tell whether to trust the value, whether to surface a
"classifier unavailable" banner, or whether to skip downstream learning
on the dismissal model. This is observability and correctness in one
bug.

The fix adds two envelope fields:

* ``source: Literal["classifier", "fallback"]`` — defaults to
  ``classifier`` for backwards compat.
* ``degraded: bool`` — True when the daemon could not run real
  inference.

Plus an ``EventType.STATE_INFER_DEGRADED`` log line so a stream
aggregator sees the degradation without inspecting the response body.

Test plan:

1. Classifier path → envelope shows ``source="classifier"`` and
   ``degraded=False``.
2. Missing scorer/smoother → envelope shows ``source="fallback"`` and
   ``degraded=True``; a STATE_INFER_DEGRADED log line is emitted with
   the bound cid.
3. The desktop dashboard's advanced tab shows the degraded banner when
   the controller forwards a payload carrying ``degraded=True``.
4. The new fields round-trip cleanly through Pydantic
   (model_validate / model_dump).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cortex.libs.logging.structured import EventType
from cortex.services.api_gateway.app import create_app, registry
from cortex.services.api_gateway.routes import StateInferResponse


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_registry() -> Any:
    registry.reset()
    yield
    registry.reset()


def _make_signal_quality() -> dict:
    return {"physio": 0.8, "kinematics": 0.7, "telemetry": 0.6}


def _make_feature_vector() -> dict:
    # Minimal valid FeatureVector — only the fields the route exercises.
    return {
        "timestamp": 1.0,
        "physio": {
            "pulse_bpm": 72.0,
            "pulse_quality": 0.9,
            "pulse_variability_proxy": 50.0,
            "hr_delta_5s": 1.0,
            "valid": True,
        },
        "kinematics": {
            "blink_rate": 16.0,
            "blink_rate_delta": -1.0,
            "blink_suppression_score": 0.0,
            "head_pitch": 1.0,
            "head_yaw": 0.0,
            "head_roll": 0.0,
            "slump_score": 0.1,
            "forward_lean_score": 0.1,
            "shoulder_drop_ratio": 0.05,
            "confidence": 0.8,
        },
        "telemetry": {
            "mouse_velocity_mean": 200.0,
            "mouse_velocity_p90": 500.0,
            "mouse_variance": 1000.0,
            "click_rate": 0.5,
            "window_switch_rate": 0.1,
            "tab_switch_rate": 0.0,
            "scroll_velocity": 50.0,
            "active_seconds": 60.0,
        },
        "context": {
            "tab_count": 5,
            "open_files_count": 3,
            "terminal_lines": 0,
            "error_indicators": 0,
            "active_application": "test",
        },
    }


# ---------------------------------------------------------------------------
# 1. Classifier path stamps source=classifier (the default)
# ---------------------------------------------------------------------------


def test_classifier_path_marks_source_classifier(
    tmp_path, monkeypatch,
) -> None:
    from cortex.libs.auth.local_token import load_or_create_token
    from cortex.services.state_engine.rule_scorer import RuleScorer
    from cortex.services.state_engine.smoother import ScoreSmoother

    token_file = tmp_path / "auth.token"
    monkeypatch.setattr(
        "cortex.libs.auth.local_token.auth_token_path", lambda: token_file
    )
    token = load_or_create_token(token_file)
    registry.register("rule_scorer", RuleScorer())
    registry.register("score_smoother", ScoreSmoother())

    app = create_app()
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {token}"
        resp = client.post(
            "/state/infer",
            json={
                "feature_vector": _make_feature_vector(),
                "signal_quality": _make_signal_quality(),
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "classifier"
    assert body["degraded"] is False


# ---------------------------------------------------------------------------
# 2. Fallback path stamps source=fallback + degraded=True + logs cid
# ---------------------------------------------------------------------------


def test_fallback_path_marks_source_fallback_and_emits_event(
    caplog: pytest.LogCaptureFixture,
    tmp_path,
    monkeypatch,
) -> None:
    """No scorer / smoother registered → envelope flags degradation and
    a structured STATE_INFER_DEGRADED line is emitted with the cid.
    """
    from cortex.libs.auth.local_token import load_or_create_token

    token_file = tmp_path / "auth.token"
    monkeypatch.setattr(
        "cortex.libs.auth.local_token.auth_token_path", lambda: token_file
    )
    token = load_or_create_token(token_file)
    app = create_app()
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {token}"
        with caplog.at_level(logging.WARNING):
            resp = client.post(
                "/state/infer",
                json={
                    "feature_vector": _make_feature_vector(),
                    "signal_quality": _make_signal_quality(),
                },
                headers={"X-Cortex-Request-ID": "cid_f18test01"},
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "fallback"
    assert body["degraded"] is True

    matching = [
        rec
        for rec in caplog.records
        if EventType.STATE_INFER_DEGRADED.value in rec.getMessage()
    ]
    assert matching, "expected a STATE_INFER_DEGRADED log line"
    msg = matching[0].getMessage()
    assert "cid=cid_f18test01" in msg


# ---------------------------------------------------------------------------
# 3. Dashboard advanced tab surfaces a degraded banner via the controller
# ---------------------------------------------------------------------------


@pytest.fixture
def qt_app():
    """Offscreen Qt app so widget rendering works in CI."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def test_advanced_tab_shows_degraded_banner_when_payload_flagged(qt_app) -> None:
    """The advanced tab's banner is visible iff the payload says so.

    This mirrors the controller signal path: the controller forwards the
    /state/infer envelope into ``dashboard.update_state(payload)``, and
    the advanced tab reads ``degraded`` / ``source`` directly off the
    dict. Qt's ``isVisible()`` is False on unshown widgets regardless of
    their explicit-visibility state, so we check ``isHidden()`` /
    ``isVisibleTo`` against the parent. The persisted explicit-show
    state is what ``update_state`` toggles.
    """
    from cortex.apps.desktop_shell.dashboard import _AdvancedTab

    tab = _AdvancedTab()
    # Default: banner explicitly hidden by ``setVisible(False)`` in
    # ``__init__``. ``isHidden()`` reflects that explicit toggle even on
    # an unrealised widget.
    assert tab._degraded_badge.isHidden() is True

    # Healthy classifier payload: still hidden.
    tab.update_state({
        "scores": {"flow": 0.7, "hypo": 0.0, "hyper": 0.2, "recovery": 0.1},
        "signal_quality": {"physio": 0.8, "kinematics": 0.7, "telemetry": 0.6},
        "confidence": 0.7,
        "dwell_seconds": 2.0,
        "state": "FLOW",
        "biometrics": {},
        "degraded": False,
        "source": "classifier",
    })
    assert tab._degraded_badge.isHidden() is True

    # Degraded payload: badge marked visible.
    tab.update_state({
        "scores": {"flow": 0.5, "hypo": 0.0, "hyper": 0.0, "recovery": 0.0},
        "signal_quality": {"physio": 0.0, "kinematics": 0.0, "telemetry": 0.0},
        "confidence": 0.5,
        "dwell_seconds": 0.0,
        "state": "FLOW",
        "biometrics": {},
        "degraded": True,
        "source": "fallback",
    })
    assert tab._degraded_badge.isHidden() is False

    # Toggling back to a healthy payload hides the banner again — the
    # transient degradation must not stick across recoveries.
    tab.update_state({
        "scores": {"flow": 0.6, "hypo": 0.0, "hyper": 0.2, "recovery": 0.2},
        "signal_quality": {"physio": 0.7, "kinematics": 0.7, "telemetry": 0.6},
        "confidence": 0.6,
        "dwell_seconds": 3.0,
        "state": "FLOW",
        "biometrics": {},
        "degraded": False,
        "source": "classifier",
    })
    assert tab._degraded_badge.isHidden() is True


# ---------------------------------------------------------------------------
# 4. Pydantic round-trip preserves the new fields
# ---------------------------------------------------------------------------


def test_state_infer_response_pydantic_round_trip() -> None:
    from cortex.libs.schemas.state import (
        SignalQuality,
        StateEstimate,
        StateScores,
    )

    estimate = StateEstimate(
        state="FLOW",
        confidence=0.5,
        scores=StateScores(flow=0.5, hypo=0.0, hyper=0.0, recovery=0.0),
        reasons=["synthetic"],
        signal_quality=SignalQuality(physio=0.0, kinematics=0.0, telemetry=0.0),
        timestamp=1.0,
        dwell_seconds=0.0,
    )

    fallback = StateInferResponse(
        estimate=estimate,
        source="fallback",
        degraded=True,
    )
    blob = fallback.model_dump_json()
    rehydrated = StateInferResponse.model_validate_json(blob)
    assert rehydrated.source == "fallback"
    assert rehydrated.degraded is True

    # Defaults match the audit prescription.
    default = StateInferResponse(estimate=estimate)
    assert default.source == "classifier"
    assert default.degraded is False
