"""Audit-2 — Bedrock 401/403 surfaces as distinct auth-error fallback.

Prior to the audit-2 sweep, ``APIStatusError(401)`` was treated like
``RateLimitError`` / ``APITimeoutError``: bounded exponential backoff
up to ``attempts``, then silent break and fall-through to the rule-based
plan. The user got delayed/missing interventions and never learned
their token had been revoked.

The fix short-circuits 401/403 to a dedicated fallback plan stamped
with ``metadata.fallback_reason == "auth_error"`` so the UI can surface
it explicitly and the dismissal model can ignore the outcome.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from cortex.libs.config.settings import LLMConfig
from cortex.libs.schemas.context import (
    EditorContext,
    TaskContext,
)
from cortex.libs.schemas.state import (
    SignalQuality,
    StateEstimate,
    StateScores,
)
from cortex.services.llm_engine.anthropic_planner import AnthropicPlanner


class _FakeAPIStatusError(Exception):
    """Stand-in for ``anthropic.APIStatusError``. The planner branches
    on ``isinstance(exc, APIStatusError)`` and ``exc.status_code``;
    monkey-patching the real symbol with this class is sufficient."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"http {status_code}")
        self.status_code = status_code


def _make_estimate() -> StateEstimate:
    return StateEstimate(
        state="HYPER",
        confidence=0.8,
        scores=StateScores(flow=0.1, hypo=0.0, hyper=0.85, recovery=0.05),
        signal_quality=SignalQuality(
            physio=0.9, kinematics=0.9, telemetry=0.9,
        ),
        dwell_seconds=12.0,
        reasons=["high HR", "rapid switching"],
        timestamp=1234.5,
    )


def _make_context() -> TaskContext:
    return TaskContext(
        mode="coding_debugging",
        active_app="vscode",
        complexity_score=0.3,
        editor_context=EditorContext(
            file_path="/src/main.py",
            visible_range=(1, 40),
            symbol_at_cursor="handle_request",
            diagnostics=[],
            recent_edits=[],
        ),
    )


def test_401_returns_auth_error_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 401 from Bedrock must short-circuit retries and return a
    fallback plan with ``metadata.fallback_reason == "auth_error"``."""
    cfg = LLMConfig()
    cfg.provider = "bedrock"

    # Build a planner with a fake SDK whose messages.create raises 401.
    class _SDK:
        def __init__(self) -> None:
            self.calls = 0
            self.messages = self

        async def create(self, **_kw: Any) -> Any:
            self.calls += 1
            raise _FakeAPIStatusError(401)

    sdk = _SDK()
    planner = AnthropicPlanner(config=cfg, sdk=sdk)

    # Swap the SDK-error class the planner catches against ours.
    import cortex.services.llm_engine.anthropic_planner as ap

    monkeypatch.setattr(ap, "APIStatusError", _FakeAPIStatusError)

    plan = asyncio.run(planner.generate_intervention_plan(
        _make_context(), _make_estimate(),
    ))

    assert plan.metadata.get("fallback_reason") == "auth_error"
    assert plan.metadata.get("source") == "fallback"
    assert sdk.calls == 1, (
        f"Auth-error fallback should NOT retry; got {sdk.calls} calls"
    )


def test_403_also_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = LLMConfig()
    cfg.provider = "bedrock"

    class _SDK:
        def __init__(self) -> None:
            self.calls = 0
            self.messages = self

        async def create(self, **_kw: Any) -> Any:
            self.calls += 1
            raise _FakeAPIStatusError(403)

    sdk = _SDK()
    planner = AnthropicPlanner(config=cfg, sdk=sdk)

    import cortex.services.llm_engine.anthropic_planner as ap

    monkeypatch.setattr(ap, "APIStatusError", _FakeAPIStatusError)

    plan = asyncio.run(planner.generate_intervention_plan(
        _make_context(), _make_estimate(),
    ))
    assert plan.metadata.get("fallback_reason") == "auth_error"
    assert plan.metadata.get("http_status") == 403
    assert sdk.calls == 1
