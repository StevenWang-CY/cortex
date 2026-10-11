"""Invalid observations return private, serializable HTTP 422 responses."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from cortex.application.services import ServiceRegistry
from cortex.libs.auth.local_token import load_or_create_token
from cortex.services.api_gateway.app import create_app


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), "private-workspace-value"])
def test_bad_feature_has_field_error_without_reflecting_input(tmp_path, monkeypatch, value) -> None:
    token_path = tmp_path / "auth.token"
    monkeypatch.setattr("cortex.libs.auth.local_token.auth_token_path", lambda: token_path)
    token = load_or_create_token(token_path)
    client = TestClient(create_app(services=ServiceRegistry()))
    response = client.post(
        "/features/telemetry",
        content=json.dumps({"mouse_velocity_mean": value}),
        headers={"Content-Type": "application/json", "X-Cortex-Auth-Token": token},
    )
    assert response.status_code == 422
    errors = response.json()["detail"]
    assert any(item["loc"] == ["body", "mouse_velocity_mean"] for item in errors)
    assert all(set(item) == {"type", "loc", "msg"} for item in errors)
    assert "private-workspace-value" not in response.text
    assert "NaN" not in response.text and "Infinity" not in response.text
    assert response.headers["X-Cortex-Request-ID"]


def test_invalid_unauthenticated_input_is_rejected_before_validation() -> None:
    client = TestClient(create_app(services=ServiceRegistry()))
    response = client.post("/features/telemetry", content='{"mouse_velocity_mean": NaN}',
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 401
