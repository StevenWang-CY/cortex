"""
Unit tests for API Gateway — FastAPI routes, service registry, WebSocket messages.

Tests verify:
- Health and status endpoints
- Feature submission endpoints (physio, kinematics, telemetry, frame_meta)
- State inference endpoint
- Context build and LLM plan endpoints (no-engine fallback)
- Intervention apply/restore endpoints (no-engine fallback)
- Service registry management
- WebSocket message serialization
- Module imports
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from cortex.libs.schemas.intervention import InterventionPlan
from cortex.libs.schemas.state import (
    SignalQuality,
    StateEstimate,
    StateScores,
)
from cortex.services.api_gateway.app import ServiceRegistry, create_app, registry
from cortex.services.api_gateway.websocket_server import WebSocketServer, WSMessage
from cortex.services.intervention_engine.executor import InterventionExecutor
from cortex.services.intervention_engine.restore import RestoreManager

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def _reset_registry():
    """Reset global registry before each test."""
    registry.reset()
    yield
    registry.reset()


@pytest.fixture()
def client() -> TestClient:
    """Create a test client for the FastAPI app."""
    app = create_app()
    with TestClient(app) as c:
        yield c


def _make_physio_features() -> dict:
    return {
        "pulse_bpm": 72.0,
        "pulse_quality": 0.8,
        "pulse_variability_proxy": 50.0,
        "hr_delta_5s": 1.0,
        "valid": True,
    }


def _make_kinematic_features() -> dict:
    return {
        "blink_rate": 16.0,
        "blink_rate_delta": -1.0,
        "blink_suppression_score": 0.0,
        "head_pitch": 2.0,
        "head_yaw": 0.0,
        "head_roll": 0.0,
        "slump_score": 0.1,
        "forward_lean_score": 0.1,
        "shoulder_drop_ratio": 0.05,
        "confidence": 0.8,
    }


def _make_telemetry_features() -> dict:
    return {
        "mouse_velocity_mean": 500.0,
        "mouse_velocity_variance": 5000.0,
        "mouse_jerk_score": 0.1,
        "click_burst_score": 0.0,
        "click_frequency": 0.5,
        "keyboard_burst_score": 0.1,
        "keystroke_interval_variance": 500.0,
        "backspace_density": 0.05,
        "inactivity_seconds": 1.0,
        "window_switch_rate": 5.0,
    }


def _make_feature_vector() -> dict:
    return {
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
    }


def _make_signal_quality() -> dict:
    return {"physio": 0.8, "kinematics": 0.7, "telemetry": 0.9}


def _make_state_estimate() -> dict:
    return {
        "state": "FLOW",
        "confidence": 0.8,
        "scores": {"flow": 0.8, "hypo": 0.0, "hyper": 0.1, "recovery": 0.0},
        "reasons": ["Stable engagement"],
        "signal_quality": _make_signal_quality(),
        "timestamp": 100.0,
        "dwell_seconds": 30.0,
    }


# =============================================================================
# Service Registry Tests
# =============================================================================


class TestServiceRegistry:
    """Test service registry management."""

    def test_register_and_get(self):
        reg = ServiceRegistry()
        reg.register("test_service", {"key": "value"})
        assert reg.get("test_service") == {"key": "value"}

    def test_get_missing_returns_none(self):
        reg = ServiceRegistry()
        assert reg.get("nonexistent") is None

    def test_has_service(self):
        reg = ServiceRegistry()
        reg.register("test", "value")
        assert reg.has("test")
        assert not reg.has("missing")

    def test_registered_services_list(self):
        reg = ServiceRegistry()
        reg.register("a", 1)
        reg.register("b", 2)
        assert sorted(reg.registered_services) == ["a", "b"]

    def test_healthy_property(self):
        reg = ServiceRegistry()
        assert not reg.healthy
        reg.healthy = True
        assert reg.healthy

    def test_reset_clears_all(self):
        reg = ServiceRegistry()
        reg.register("test", "value")
        reg.healthy = True
        reg.reset()
        assert not reg.has("test")
        assert not reg.healthy


# =============================================================================
# Health & Status Endpoint Tests
# =============================================================================


class TestHealthEndpoint:
    """Test /health endpoint."""

    def test_health_returns_200(self, client: TestClient):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert "uptime_seconds" in data
        assert isinstance(data["services"], dict)

    def test_health_shows_registered_services(self, client: TestClient):
        registry.register("capture", object())
        registry.register("physio", object())
        resp = client.get("/health")
        data = resp.json()
        assert "capture" in data["services"]
        assert "physio" in data["services"]


class TestStatusEndpoint:
    """Test /status/current endpoint."""

    def test_status_empty(self, client: TestClient):
        resp = client.get("/status/current")
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] is None

    def test_status_with_stored_estimate(self, client: TestClient):
        estimate = StateEstimate(
            state="HYPER",
            confidence=0.9,
            scores=StateScores(flow=0.05, hypo=0.0, hyper=0.9, recovery=0.0),
            reasons=["Test"],
            signal_quality=SignalQuality(physio=0.8, kinematics=0.7, telemetry=0.9),
            timestamp=100.0,
            dwell_seconds=10.0,
        )
        registry.register("latest_state_estimate", estimate)
        resp = client.get("/status/current")
        data = resp.json()
        assert data["state"] == "HYPER"
        assert data["confidence"] == 0.9


# =============================================================================
# Feature Submission Endpoint Tests
# =============================================================================


class TestFeatureEndpoints:
    """Test feature submission endpoints."""

    def test_submit_frame_meta(self, client: TestClient):
        payload = {
            "timestamp": 1.0,
            "face_detected": True,
            "face_confidence": 0.95,
            "brightness_score": 0.8,
            "blur_score": 0.7,
            "motion_score": 0.9,
        }
        resp = client.post("/capture/frame_meta", json=payload)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_submit_physio_features(self, client: TestClient):
        resp = client.post("/features/physio", json=_make_physio_features())
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        # Verify stored in registry
        assert registry.has("latest_physio")

    def test_submit_kinematic_features(self, client: TestClient):
        resp = client.post("/features/kinematics", json=_make_kinematic_features())
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        assert registry.has("latest_kinematics")

    def test_submit_telemetry_features(self, client: TestClient):
        resp = client.post("/features/telemetry", json=_make_telemetry_features())
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        assert registry.has("latest_telemetry")

    def test_invalid_physio_rejected(self, client: TestClient):
        resp = client.post("/features/physio", json={"invalid": True})
        assert resp.status_code == 422  # Validation error


# =============================================================================
# State Inference Endpoint Tests
# =============================================================================


class TestStateInferEndpoint:
    """Test /state/infer endpoint."""

    def test_infer_without_engine_returns_default(self, client: TestClient):
        """Without registered engines, should return default FLOW state."""
        payload = {
            "feature_vector": _make_feature_vector(),
            "signal_quality": _make_signal_quality(),
        }
        resp = client.post("/state/infer", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["estimate"]["state"] == "FLOW"
        assert "No state engine registered" in data["estimate"]["reasons"][0]

    def test_infer_with_engines(self, client: TestClient):
        """With registered engines, should use them."""
        from cortex.services.state_engine.rule_scorer import RuleScorer
        from cortex.services.state_engine.smoother import ScoreSmoother

        registry.register("rule_scorer", RuleScorer())
        registry.register("score_smoother", ScoreSmoother())

        payload = {
            "feature_vector": _make_feature_vector(),
            "signal_quality": _make_signal_quality(),
        }
        resp = client.post("/state/infer", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        # Should use real engines, not fallback
        est = data["estimate"]
        assert est["state"] in ("FLOW", "HYPO", "HYPER", "RECOVERY")
        assert 0.0 <= est["confidence"] <= 1.0


# =============================================================================
# Context & LLM Endpoints (No-engine fallback)
# =============================================================================


class TestContextAndLLMEndpoints:
    """Test context and LLM endpoints in fallback mode."""

    def test_build_context_no_engine(self, client: TestClient):
        payload = {
            "include_editor": True,
            "include_terminal": True,
            "include_browser": True,
        }
        resp = client.post("/context/build", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is False

    def test_llm_plan_no_engine(self, client: TestClient):
        payload = {
            "state_estimate": _make_state_estimate(),
            "task_context": {
                "mode": "coding_debugging",
                "active_app": "vscode",
                "complexity_score": 0.7,
            },
        }
        resp = client.post("/llm/plan", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["fallback_used"] is True

    def test_llm_plan_with_generate_intervention_plan_client(self, client: TestClient):
        class MockLLMClient:
            async def generate_intervention_plan(self, context, state):
                return InterventionPlan(
                    level="overlay_only",
                    situation_summary="Focused plan",
                    headline="Do the next thing",
                    primary_focus="Read the current error",
                    micro_steps=["Inspect the latest stack trace"],
                    ui_plan={"show_overlay": True, "intervention_type": "overlay_only"},
                    tone="direct",
                )

        registry.register("llm_engine", MockLLMClient())

        payload = {
            "state_estimate": _make_state_estimate(),
            "task_context": {
                "mode": "coding_debugging",
                "active_app": "vscode",
                "complexity_score": 0.7,
            },
        }
        resp = client.post("/llm/plan", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["fallback_used"] is False
        assert data["plan"]["headline"] == "Do the next thing"


# =============================================================================
# Intervention Endpoints (No-engine fallback)
# =============================================================================


class TestInterventionEndpoints:
    """Test intervention endpoints in fallback mode."""

    def test_apply_no_engine(self, client: TestClient):
        payload = {
            "plan": {
                "level": "overlay_only",
                "situation_summary": "User appears overwhelmed",
                "headline": "Take a breath",
                "primary_focus": "Current task",
                "micro_steps": ["Step 1"],
                "ui_plan": {
                    "dim_background": False,
                    "show_overlay": True,
                    "fold_unrelated_code": False,
                    "intervention_type": "overlay_only",
                },
                "tone": "supportive",
            },
        }
        resp = client.post("/intervention/apply", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["applied"] is False

    def test_restore_no_engine(self, client: TestClient):
        payload = {
            "intervention_id": "int_abc123",
            "user_action": "dismissed",
        }
        resp = client.post("/intervention/restore", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["restored"] is False

    def test_apply_with_executor_and_restore_manager(self, client: TestClient):
        class OverlayAdapter:
            async def execute(self, action: str, params: dict) -> bool:
                return action in {"show_overlay", "hide_overlay"}

        executor = InterventionExecutor()
        executor.register_adapter("overlay", OverlayAdapter())
        restore_manager = RestoreManager(executor)
        registry.register("intervention_executor", executor)
        registry.register("restore_manager", restore_manager)

        payload = {
            "plan": {
                "intervention_id": "int_apply_123",
                "level": "overlay_only",
                "situation_summary": "User appears overwhelmed",
                "headline": "Focus on one error",
                "primary_focus": "Read the latest stack trace",
                "micro_steps": ["Inspect the latest stack trace"],
                "ui_plan": {
                    "dim_background": False,
                    "show_overlay": True,
                    "fold_unrelated_code": False,
                    "intervention_type": "overlay_only",
                },
                "tone": "supportive",
            },
        }
        resp = client.post("/intervention/apply", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["applied"] is True
        assert data["snapshot"]["intervention_id"] == "int_apply_123"
        assert restore_manager.active_count == 1

    def test_restore_with_restore_manager(self, client: TestClient):
        class OverlayAdapter:
            async def execute(self, action: str, params: dict) -> bool:
                return action in {"show_overlay", "hide_overlay"}

        executor = InterventionExecutor()
        executor.register_adapter("overlay", OverlayAdapter())
        restore_manager = RestoreManager(executor)
        registry.register("intervention_executor", executor)
        registry.register("restore_manager", restore_manager)

        apply_payload = {
            "plan": {
                "intervention_id": "int_restore_123",
                "level": "overlay_only",
                "situation_summary": "User appears overwhelmed",
                "headline": "Focus on one error",
                "primary_focus": "Read the latest stack trace",
                "micro_steps": ["Inspect the latest stack trace"],
                "ui_plan": {
                    "dim_background": False,
                    "show_overlay": True,
                    "fold_unrelated_code": False,
                    "intervention_type": "overlay_only",
                },
                "tone": "supportive",
            },
        }
        apply_resp = client.post("/intervention/apply", json=apply_payload)
        assert apply_resp.status_code == 200

        restore_resp = client.post(
            "/intervention/restore",
            json={"intervention_id": "int_restore_123", "user_action": "dismissed"},
        )
        assert restore_resp.status_code == 200
        data = restore_resp.json()
        assert data["restored"] is True
        assert data["outcome"]["intervention_id"] == "int_restore_123"
        assert restore_manager.active_count == 0


# =============================================================================
# WebSocket Message Tests
# =============================================================================


class TestWSMessage:
    """Test WebSocket message serialization."""

    def test_to_json(self):
        msg = WSMessage(
            type="STATE_UPDATE",
            payload={"state": "FLOW", "confidence": 0.8},
            timestamp=100.0,
            sequence=1,
        )
        raw = msg.to_json()
        parsed = json.loads(raw)
        assert parsed["type"] == "STATE_UPDATE"
        assert parsed["payload"]["state"] == "FLOW"
        assert parsed["sequence"] == 1

    def test_from_json(self):
        raw = json.dumps({
            "type": "USER_ACTION",
            "payload": {"action": "dismissed", "intervention_id": "int_123"},
            "timestamp": 100.0,
            "sequence": 5,
        })
        msg = WSMessage.from_json(raw)
        assert msg.type == "USER_ACTION"
        assert msg.payload["action"] == "dismissed"
        assert msg.sequence == 5

    def test_roundtrip(self):
        original = WSMessage(
            type="INTERVENTION_TRIGGER",
            payload={"headline": "Focus on one thing"},
            timestamp=200.0,
            sequence=10,
        )
        restored = WSMessage.from_json(original.to_json())
        assert restored.type == original.type
        assert restored.payload == original.payload
        assert restored.sequence == original.sequence


class TestWebSocketServer:
    """Test WebSocket server state management."""

    def test_initial_state(self):
        server = WebSocketServer()
        assert server.client_count == 0
        assert not server.is_running
        assert server.connected_clients == []

    def test_reset(self):
        server = WebSocketServer()
        server._sequence = 42
        server._latest_state = StateEstimate(
            state="HYPER",
            confidence=0.9,
            scores=StateScores(flow=0.0, hypo=0.0, hyper=0.9, recovery=0.0),
            reasons=[],
            signal_quality=SignalQuality(physio=0.8, kinematics=0.7, telemetry=0.9),
            timestamp=1.0,
            dwell_seconds=0.0,
        )
        server.reset()
        assert server._sequence == 0
        assert server._latest_state is None

    def test_make_state_update_message(self):
        server = WebSocketServer()
        estimate = StateEstimate(
            state="FLOW",
            confidence=0.85,
            scores=StateScores(flow=0.85, hypo=0.0, hyper=0.1, recovery=0.0),
            reasons=["Stable engagement"],
            signal_quality=SignalQuality(physio=0.8, kinematics=0.7, telemetry=0.9),
            timestamp=100.0,
            dwell_seconds=30.0,
        )
        msg = server._make_state_update(estimate)
        assert msg.type == "STATE_UPDATE"
        assert msg.payload["state"] == "FLOW"
        assert msg.payload["confidence"] == 0.85
        assert msg.payload["dwell_seconds"] == 30.0
        assert "signal_quality" in msg.payload
        assert msg.sequence == 1

    def test_make_intervention_trigger_message(self):
        from cortex.libs.schemas.intervention import InterventionPlan, UIPlan

        server = WebSocketServer()
        plan = InterventionPlan(
            level="overlay_only",
            situation_summary="User appears overwhelmed",
            headline="Take a breath",
            primary_focus="Current task",
            micro_steps=["Step 1", "Step 2"],
            ui_plan=UIPlan(
                dim_background=False,
                show_overlay=True,
                fold_unrelated_code=False,
                intervention_type="overlay_only",
            ),
            tone="supportive",
        )
        msg = server._make_intervention_trigger(plan)
        assert msg.type == "INTERVENTION_TRIGGER"
        assert msg.payload["headline"] == "Take a breath"
        assert len(msg.payload["micro_steps"]) == 2
        assert msg.sequence == 1

    def test_sequence_increments(self):
        server = WebSocketServer()
        estimate = StateEstimate(
            state="FLOW",
            confidence=0.8,
            scores=StateScores(flow=0.8, hypo=0.0, hyper=0.0, recovery=0.0),
            reasons=[],
            signal_quality=SignalQuality(physio=0.8, kinematics=0.7, telemetry=0.9),
            timestamp=1.0,
            dwell_seconds=0.0,
        )
        msg1 = server._make_state_update(estimate)
        msg2 = server._make_state_update(estimate)
        assert msg2.sequence == msg1.sequence + 1


# =============================================================================
# Module Import Tests
# =============================================================================


class TestAPIGatewayImports:
    """Test that all API gateway exports are importable."""

    def test_import_create_app(self):
        from cortex.services.api_gateway import create_app
        assert create_app is not None

    def test_import_registry(self):
        from cortex.services.api_gateway import ServiceRegistry, registry
        assert ServiceRegistry is not None
        assert registry is not None

    def test_import_websocket_server(self):
        from cortex.services.api_gateway import WebSocketServer, WSMessage

        assert WSMessage is not None
        assert WebSocketServer is not None
