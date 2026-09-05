from __future__ import annotations

from cortex.apps.desktop_shell.view_models import (
    advanced_state_view,
    consumer_state_view,
)


def test_consumer_view_prioritizes_capture_health_and_abstention() -> None:
    view = consumer_state_view(
        {
            "state": "FLOW",
            "status": "insufficient_evidence",
            "capture": {
                "stale": True,
                "frames_flowing": False,
                "face_detected": False,
            },
            "store": {"degraded": True},
            "biometrics": {"heart_rate": None},
            "connected_clients": ["Chrome", "vscode", 42],
        }
    )

    assert view.state == "UNKNOWN"
    assert view.label == "Not enough evidence"
    assert view.health_message == "Camera offline — frames are not flowing"
    assert view.biometrics_status is not None
    assert view.biometrics_status.startswith("Camera offline")
    assert view.connected_surfaces == frozenset({"chrome", "vscode"})


def test_advanced_view_uses_canonical_scores_and_bounds_transport_numbers() -> None:
    view = advanced_state_view(
        {
            "state": "RECOVERY",
            "status": "estimated",
            "support_scores": {"flow_like": 0.7, "support_likely": "bad"},
            "scores": {"flow": 0.1, "hyper": 0.8, "hypo": 0.2},
            "confidence": 4,
            "evidence_coverage": -2,
            "dwell_seconds": "12.5",
            "biometrics": {"heart_rate": "72"},
        }
    )

    assert view.scores == {
        "flow": 0.7,
        "hyper": 0.0,
        "hypo": 0.2,
        "recovery": 0.0,
    }
    assert view.evidence_strength == 1.0
    assert view.evidence_coverage == 0.0
    assert view.dwell_seconds == 12.5
    assert view.heart_rate == 72.0
    assert view.degraded_message is None


def _view_with_reason(reason: dict[str, object] | None) -> str | None:
    capture: dict[str, object] = {"stale": False, "frames_flowing": True, "face_detected": True}
    if reason is not None:
        capture["pulse_unavailable"] = reason
    return consumer_state_view(
        {"state": "FLOW", "status": "warming_up", "capture": capture, "biometrics": {}}
    ).biometrics_status


def test_pulse_copy_states_the_real_blocker_instead_of_a_permanent_warm_up() -> None:
    assert _view_with_reason(None) == "Reading your pulse…"
    assert _view_with_reason(
        {"code": "filling", "message": "filling 4.2/10 s", "observed": 4.2, "required": 10.0}
    ) == "Reading your pulse… 4 of 10 s"
    assert _view_with_reason(
        {
            "code": "valid_fraction_below_gate",
            "message": "93/300 scheduled frames unusable (31%): face not detected",
            "missing_reason": "NO_FACE",
        }
    ) == "Stay in view for a pulse reading"
    assert _view_with_reason(
        {"code": "low_light", "message": "low light", "missing_reason": "LOW_LIGHT"}
    ).startswith("Too dark")
    assert _view_with_reason(
        {"code": "motion_fraction_above_cap", "message": "motion 14% > 10%"}
    ) == "Hold still for a pulse reading"
    assert _view_with_reason({"code": "no_observations", "message": "none yet"}) == (
        "Waiting for the camera…"
    )
    # Unknown or malformed reasons fall back to the neutral copy, never a crash.
    assert _view_with_reason({"code": "something_new"}) == "Reading your pulse…"
    assert _view_with_reason("garbage") == "Reading your pulse…"  # type: ignore[arg-type]
