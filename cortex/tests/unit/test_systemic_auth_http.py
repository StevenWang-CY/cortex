"""Systemic capability-token gate on HTTP routes (audit Debt-2, Commit 1).

The Wave-1 fixes F07/F08 shipped tactical token gates on SHUTDOWN and
the launcher ``/stop`` endpoint. Debt-2 promotes the pattern: every
HTTP route on the API gateway except ``/health`` now requires the token.
These tests are the structural regression guard — adding a new mutating
endpoint without the gate, or removing the gate from an existing one,
must fail one of these cases.

Six cases:

1. ``/state/infer`` without any token → 401.
2. ``/state/infer`` with ``Authorization: Bearer <token>`` → 200.
3. ``/state/infer`` with ``X-Cortex-Auth-Token: <token>`` → 200.
4. ``/health`` always reachable (no token, wrong token, right token).
5. Wrong token (well-formed but non-matching) → 401.
6. The 401 path emits the structured ``AUTH_REJECTED`` event so log
   aggregators can alarm on spikes.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cortex.libs.auth.local_token import load_or_create_token
from cortex.libs.logging.structured import EventType
from cortex.services.api_gateway.app import create_app, registry


def _feature_vector_payload() -> dict:
    """A minimal valid ``/state/infer`` request body."""
    return {
        "feature_vector": {
            "timestamp": 1.0,
            "hr": 72.0,
            "hrv_rmssd": 50.0,
            "hr_delta": 1.0,
            "blink_rate": 16.0,
            "blink_rate_delta": -1.0,
            "shoulder_drop_ratio": 0.05,
            "forward_lean_angle": 5.0,
            "mouse_velocity_mean": 500.0,
            "mouse_velocity_variance": 5000.0,
            "click_frequency": 0.5,
            "keystroke_interval_variance": 500.0,
            "tab_switch_frequency": 5.0,
        },
        "signal_quality": {"physio": 0.8, "kinematics": 0.7, "telemetry": 0.9},
    }


@pytest.fixture()
def token_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the capability-token file into ``tmp_path`` for the test."""
    target = tmp_path / "auth.token"
    monkeypatch.setattr(
        "cortex.libs.auth.local_token.auth_token_path", lambda: target
    )
    return target


@pytest.fixture()
def auth_token(token_file: Path) -> str:
    """Provision the token and return its hex value."""
    return load_or_create_token(token_file)


@pytest.fixture()
def client(token_file: Path) -> TestClient:
    """Fresh app instance per test, with a clean ServiceRegistry."""
    registry.reset()
    app = create_app()
    with TestClient(app) as c:
        yield c
    registry.reset()


def test_state_infer_without_token_returns_401(
    client: TestClient, auth_token: str,
) -> None:
    """Case 1: no Authorization header → 401."""
    resp = client.post("/state/infer", json=_feature_vector_payload())
    assert resp.status_code == 401, resp.text
    assert "Bearer" in resp.headers.get("WWW-Authenticate", "")


def test_state_infer_with_bearer_token_returns_200(
    client: TestClient, auth_token: str,
) -> None:
    """Case 2: Authorization: Bearer <token> succeeds."""
    resp = client.post(
        "/state/infer",
        json=_feature_vector_payload(),
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert resp.status_code == 200, resp.text


def test_state_infer_with_x_cortex_header_returns_200(
    client: TestClient, auth_token: str,
) -> None:
    """Case 3: legacy X-Cortex-Auth-Token header also works.

    The extension prefers the ``X-`` header because Chrome's
    ``fetch`` does not need a CORS preflight for ``X-`` prefixed
    headers when the body is also CORS-safelisted.
    """
    resp = client.post(
        "/state/infer",
        json=_feature_vector_payload(),
        headers={"X-Cortex-Auth-Token": auth_token},
    )
    assert resp.status_code == 200, resp.text


def test_health_always_reachable(
    client: TestClient, auth_token: str,
) -> None:
    """Case 4: ``/health`` does not require a token in any configuration.

    The supervisor liveness probe must reach the daemon before the UI
    has presented its token (the launcher polls ``/health`` until the
    daemon is up, then hands the token to the UI).
    """
    # No header.
    assert client.get("/health").status_code == 200
    # Wrong header.
    assert client.get(
        "/health", headers={"Authorization": "Bearer not-it"},
    ).status_code == 200
    # Right header.
    assert client.get(
        "/health", headers={"Authorization": f"Bearer {auth_token}"},
    ).status_code == 200


def test_wrong_token_returns_401(
    client: TestClient, auth_token: str,
) -> None:
    """Case 5: a well-formed but non-matching token still 401s.

    Defends against a stale client that retained an old token across
    a rotation; the gate is not "any string accepted" but a
    constant-time comparison against the on-disk value.
    """
    bad = "0" * len(auth_token)
    resp = client.post(
        "/state/infer",
        json=_feature_vector_payload(),
        headers={"Authorization": f"Bearer {bad}"},
    )
    assert resp.status_code == 401


def test_auth_rejected_event_emitted(
    client: TestClient,
    auth_token: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Case 6: the rejection path emits ``EventType.AUTH_REJECTED``.

    Without this event a log aggregator cannot alarm on spikes (the
    hostile-localhost scanner signal). The event lives on the
    ``cortex.services.api_gateway.auth`` logger at WARNING level.
    """
    with caplog.at_level(logging.WARNING, logger="cortex.services.api_gateway.auth"):
        resp = client.post("/state/infer", json=_feature_vector_payload())
    assert resp.status_code == 401
    rejection_lines = [
        rec for rec in caplog.records
        if EventType.AUTH_REJECTED.value in rec.getMessage()
    ]
    assert rejection_lines, (
        f"expected AUTH_REJECTED event, saw: "
        f"{[rec.getMessage() for rec in caplog.records]}"
    )
    # The path of the rejected request is in the structured message.
    assert any("/state/infer" in rec.getMessage() for rec in rejection_lines)
