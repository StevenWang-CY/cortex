"""Containment tests for unvalidated HRV-derived break behavior."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from cortex.libs.auth.local_token import load_or_create_token
from cortex.libs.config.settings import InterventionConfig
from cortex.services.api_gateway.app import create_app, registry


def test_biology_break_cannot_be_enabled_by_legacy_config() -> None:
    assert InterventionConfig().enable_biology_break is False
    assert InterventionConfig(enable_biology_break=True).enable_biology_break is False


def test_stress_endpoint_is_unavailable_and_side_effect_free(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    tracker = MagicMock()
    tracker.should_break.side_effect = AssertionError("must not inspect trigger state")
    old_tracker = registry.get("stress_integral_tracker")
    registry.register("stress_integral_tracker", tracker)
    token_path = tmp_path / "auth.token"
    monkeypatch.setattr(  # type: ignore[attr-defined]
        "cortex.libs.auth.local_token.auth_token_path",
        lambda: token_path,
    )
    token = load_or_create_token(token_path)

    try:
        with TestClient(create_app()) as client:
            response = client.get(
                "/api/stress-integral",
                headers={"Authorization": f"Bearer {token}"},
            )
    finally:
        if old_tracker is None:
            registry._services.pop("stress_integral_tracker", None)
        else:
            registry.register("stress_integral_tracker", old_tracker)

    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
    assert response.json()["unavailable_reason"] == "validation_required"
    assert response.json()["should_break"] is False
    tracker.should_break.assert_not_called()
