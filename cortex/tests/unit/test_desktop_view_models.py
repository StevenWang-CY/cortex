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
