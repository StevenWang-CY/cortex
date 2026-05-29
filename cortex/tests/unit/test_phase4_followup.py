"""Phase 4 follow-up — daemon-side wiring for desktop shell expectations.

Tasks covered:
* §3.15: COST_REQUEST / COST_RESPONSE round-trip
* §3.19: TEST_PROVIDER round-trip (rule_based short-circuit)
* §3.20: weekly_schedule suppresses a Monday-morning trigger
* §3.13: GOAL_SET stamps ``goal_title`` on SessionReport
* §3.21: FORCE_RECAP / DISMISS_OVERLAY ack contracts
* §3.24: POST /api/feedback persists scrubbed JSON
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

# ─── §3.15 cost + §3.19 test_provider envelopes ──────────────────────


def test_cost_response_envelope_dumps_clean_keys() -> None:
    from cortex.libs.schemas.realtime import CostResponse

    payload = CostResponse(
        cost_today=1.23, budget_today=20.0, provider="bedrock",
        budget_exhausted=False,
    ).model_dump(mode="json")
    assert payload["cost_today"] == 1.23
    assert payload["budget_today"] == 20.0
    assert payload["provider"] == "bedrock"
    assert payload["budget_exhausted"] is False


def test_test_provider_result_envelope() -> None:
    from cortex.libs.schemas.realtime import TestProviderResult

    payload = TestProviderResult(
        provider="rule_based", ok=True, latency_ms=0.0, error=None,
    ).model_dump(mode="json")
    assert payload == {
        "provider": "rule_based",
        "ok": True,
        "latency_ms": 0.0,
        "error": None,
    }


# ─── §3.20 weekly_schedule suppression ──────────────────────────────


def test_weekly_schedule_monday_morning_off_blocks_trigger() -> None:
    """P0 §3.20: a HYPER trigger on a Monday-9am instant with
    ``monday[morning] = "off"`` must be suppressed with reason
    ``weekly_schedule_off``.
    """
    import datetime as _dt
    from unittest.mock import patch

    from cortex.libs.config.settings import InterventionConfig, StateConfig
    from cortex.libs.schemas.state import (
        SignalQuality,
        StateEstimate,
        StateScores,
    )
    from cortex.services.state_engine.trigger_policy import TriggerPolicy

    cfg = InterventionConfig(cooldown_seconds=0, max_interventions_per_hour=0)
    p = TriggerPolicy(
        config=cfg, state_config=StateConfig(hyper_dwell_seconds=5),
    )
    p.set_weekly_schedule({
        "monday": ["off", "on", "on", "on"],
        "tuesday": ["on", "on", "on", "on"],
    })
    est = StateEstimate(
        state="HYPER",
        confidence=0.9,
        scores=StateScores(flow=0.05, hypo=0.05, hyper=0.85, recovery=0.05),
        reasons=["test"],
        signal_quality=SignalQuality(physio=0.9, kinematics=0.9, telemetry=0.9),
        timestamp=0.0,
        dwell_seconds=30.0,
    )

    # Patch datetime.now() inside the trigger_policy module to a Monday
    # at 09:00 local time.
    monday_morning = _dt.datetime(2024, 1, 1, 9, 0)  # 2024-01-01 is Monday

    class _FakeDT(_dt.datetime):
        @classmethod
        def now(cls, tz: Any = None) -> _dt.datetime:  # type: ignore[override]
            return monday_morning

        @classmethod
        def fromtimestamp(cls, ts: float, tz: Any = None) -> _dt.datetime:  # type: ignore[override]
            return monday_morning

    with patch.object(_dt, "datetime", _FakeDT):
        decision = p.evaluate(est, current_time=1000.0)

    assert decision.should_trigger is False, decision.reason
    assert decision.reason == "weekly_schedule_off"


def test_weekly_schedule_empty_schedule_does_not_block() -> None:
    from cortex.libs.config.settings import InterventionConfig, StateConfig
    from cortex.services.state_engine.trigger_policy import TriggerPolicy

    p = TriggerPolicy(
        config=InterventionConfig(cooldown_seconds=0),
        state_config=StateConfig(hyper_dwell_seconds=5),
    )
    # Empty schedule → lookup returns None
    assert p.lookup_schedule_slot() is None
    # Garbage input clears the schedule
    p.set_weekly_schedule({"badday": ["on", "on", "on", "on"]})  # type: ignore[arg-type]
    assert p.lookup_schedule_slot() is None


# ─── §3.13 goal stamping ────────────────────────────────────────────


def test_session_report_stamps_goal_title() -> None:
    """P0 §3.13: a SessionReportGenerator stamped via ``set_goal_title``
    persists the goal onto the resulting SessionReport.
    """
    from cortex.services.session_report.generator import (
        SessionReportGenerator,
    )

    gen = SessionReportGenerator()
    gen.start()
    gen.set_goal_title("  finish PR review  ")
    report = gen.finish()
    assert report.goal_title == "finish PR review"


def test_session_report_goal_title_none_clears() -> None:
    from cortex.services.session_report.generator import (
        SessionReportGenerator,
    )

    gen = SessionReportGenerator()
    gen.start()
    gen.set_goal_title("draft")
    gen.set_goal_title(None)
    report = gen.finish()
    assert report.goal_title is None


# ─── §3.19 daemon.test_provider (rule_based path) ────────────────────


@pytest.mark.asyncio
async def test_test_provider_rule_based_short_circuits() -> None:
    """The ``rule_based`` provider must skip the network probe and reply
    ``ok=True, latency_ms=0`` immediately so the settings UI can confirm
    the deterministic fallback path is wired without a credential.
    """
    from cortex.services.runtime_daemon import CortexDaemon

    class _Stub:
        config = type("C", (), {"llm": type("L", (), {"provider": "bedrock"})()})()

        async def test_provider(self, provider: str) -> Any:
            return await CortexDaemon.test_provider(self, provider)

    stub = _Stub()
    result = await stub.test_provider("rule_based")
    assert result.ok is True
    assert result.latency_ms == 0.0
    assert result.provider == "rule_based"


# ─── §3.15 daemon.get_cost_response ──────────────────────────────────


@pytest.mark.asyncio
async def test_get_cost_response_reads_tracker_total() -> None:
    """``get_cost_response`` should resolve the planner's
    ``_cost_tracker`` and return a CostResponse with today's spend.
    """
    from cortex.services.runtime_daemon import CortexDaemon

    class _Tracker:
        def today_total_usd(self) -> float:
            return 0.42

        def check_budget(self) -> str:
            return "OK"

    class _Planner:
        _cost_tracker = _Tracker()

    class _LLMConfig:
        provider = "bedrock"
        daily_cost_budget_usd = 20.0

    class _Config:
        llm = _LLMConfig()

    class _Stub:
        config = _Config()
        _llm_client = _Planner()

        async def get_cost_response(self) -> Any:
            return await CortexDaemon.get_cost_response(self)

    stub = _Stub()
    payload = await stub.get_cost_response()
    assert payload.cost_today == pytest.approx(0.42)
    assert payload.budget_today == 20.0
    assert payload.provider == "bedrock"
    assert payload.budget_exhausted is False


# ─── §3.21 force_recap / dismiss_active_overlay ──────────────────────


@pytest.mark.asyncio
async def test_force_recap_no_active_session_broadcasts_synth() -> None:
    """When no session is active, ``force_recap`` should still broadcast
    a minimal synthesised recap with ``persisted=False``."""
    from cortex.libs.schemas.ws_message_types import MessageType
    from cortex.services.runtime_daemon import CortexDaemon

    sent: list[tuple[str, dict]] = []

    class _WS:
        async def send_message(self, t: str, p: dict, **_kw: Any) -> int:
            sent.append((t, p))
            return 1

    class _Stub:
        _ws_server = _WS()
        _session_report_started = False
        _session_report = None
        _latest_session_recap: dict | None = None

        async def force_recap(self) -> bool:
            return await CortexDaemon.force_recap(self)

    stub = _Stub()
    ok = await stub.force_recap()
    assert ok is True
    assert len(sent) == 1
    msg_type, payload = sent[0]
    assert msg_type == MessageType.SESSION_RECAP.value
    assert payload["persisted"] is False
    assert payload["session_id"] == "force_recap"


@pytest.mark.asyncio
async def test_dismiss_active_overlay_clears_state_and_broadcasts() -> None:
    from cortex.libs.schemas.ws_message_types import MessageType
    from cortex.services.runtime_daemon import CortexDaemon

    sent: list[tuple[str, dict]] = []

    class _WS:
        async def send_message(self, t: str, p: dict, **_kw: Any) -> int:
            sent.append((t, p))
            return 1

    class _Stub:
        _ws_server = _WS()
        _active_intervention_id: str | None = "iv-99"
        _active_plan: Any = object()

        async def dismiss_active_overlay(self) -> bool:
            return await CortexDaemon.dismiss_active_overlay(self)

    stub = _Stub()
    ok = await stub.dismiss_active_overlay()
    assert ok is True
    assert stub._active_intervention_id is None
    assert stub._active_plan is None
    msg_type, payload = sent[0]
    assert msg_type == MessageType.DISMISS_OVERLAY.value
    assert payload["intervention_id"] == "iv-99"
    assert payload["reason"] == "user_shortcut"


# ─── §3.24 feedback endpoint ─────────────────────────────────────────


def test_feedback_endpoint_persists_scrubbed_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/feedback writes a sanitised JSON record under
    ``<config_dir>/feedback/`` and returns ``ok=True``.
    """
    from fastapi.testclient import TestClient

    # Patch get_config_dir BEFORE building the app so the feedback dir
    # lands inside ``tmp_path``.
    monkeypatch.setattr(
        "cortex.libs.utils.platform.get_config_dir",
        lambda: tmp_path,
    )

    from cortex.libs.config.settings import APIConfig
    from cortex.services.api_gateway.app import create_app

    cfg = APIConfig()
    # Bypass the capability-token gate by including a valid token. We
    # mint a fresh token and pass it via the header the gate expects.
    from cortex.libs.auth import load_or_create_token

    token = load_or_create_token()
    app = create_app(config=cfg)
    client = TestClient(app)
    resp = client.post(
        "/api/feedback",
        json={
            "description": "x" * 12,  # >=10 chars
            "include_logs": False,
            "app_version": "1.2.3",
        },
        headers={"X-Cortex-Auth-Token": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["report_id"]
    feedback_dir = tmp_path / "feedback"
    assert feedback_dir.exists()
    files = list(feedback_dir.glob("*.json"))
    assert len(files) == 1
    import json
    record = json.loads(files[0].read_text())
    assert record["description"] == "x" * 12
    assert record["app_version"] == "1.2.3"
    assert record["report_id"] == body["report_id"]


def test_feedback_endpoint_persists_user_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C2: POST /api/feedback persists the ``user_agent`` the popup sends
    (navigator.userAgent) into the stored record so support can triage by
    browser/OS without round-tripping the user."""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(
        "cortex.libs.utils.platform.get_config_dir",
        lambda: tmp_path,
    )

    from cortex.libs.auth import load_or_create_token
    from cortex.libs.config.settings import APIConfig
    from cortex.services.api_gateway.app import create_app

    token = load_or_create_token()
    app = create_app(config=APIConfig())
    client = TestClient(app)
    ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/120.0"
    resp = client.post(
        "/api/feedback",
        json={
            "description": "y" * 15,
            "include_logs": False,
            "app_version": "2.0.0",
            "user_agent": ua,
        },
        headers={"X-Cortex-Auth-Token": token},
    )
    assert resp.status_code == 200, resp.text
    import json
    files = list((tmp_path / "feedback").glob("*.json"))
    assert len(files) == 1
    record = json.loads(files[0].read_text())
    assert record["user_agent"] == ua
    assert record["app_version"] == "2.0.0"


def test_feedback_endpoint_scrub_helpers_redact_paths_and_tokens() -> None:
    from cortex.services.api_gateway.routes import _scrub_log_tail

    raw = [
        "GET /api/foo header X-Cortex-Auth: abc123secret OK",
        "file: /Users/jdoe/Library/Logs/Cortex/cortex_daemon.log",
        "ordinary line nothing to redact",
    ]
    out = _scrub_log_tail(raw)
    assert "abc123secret" not in out[0]
    assert "[REDACTED]" in out[0]
    assert "jdoe" not in out[1]
    assert "/Users/[REDACTED]" in out[1]
    assert out[2] == raw[2]
