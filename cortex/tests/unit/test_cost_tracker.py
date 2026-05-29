"""Unit tests for the F20 per-call LLM cost telemetry + kill-switch.

The tests run entirely in-process: a temp directory holds the cost
ledger, a stub Anthropic SDK feeds canned ``usage`` payloads, and time
is advanced via ``datetime`` injection. No network or keychain access.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from cortex.libs.config.settings import BedrockConfig, LLMConfig
from cortex.libs.llm.pricing import usd_cost
from cortex.libs.schemas.context import EditorContext, TaskContext
from cortex.libs.schemas.state import SignalQuality, StateEstimate, StateScores
from cortex.services.llm_engine.anthropic_planner import AnthropicPlanner
from cortex.services.llm_engine.cost_tracker import CostTracker

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_context() -> TaskContext:
    return TaskContext(
        mode="coding_debugging",
        active_app="vscode",
        complexity_score=0.6,
        editor_context=EditorContext(
            file_path="/src/main.py",
            visible_range=(1, 40),
            symbol_at_cursor="handle_request",
            diagnostics=[],
            recent_edits=[],
        ),
    )


def _make_state() -> StateEstimate:
    return StateEstimate(
        state="HYPER",
        confidence=0.9,
        scores=StateScores(flow=0.05, hypo=0.0, hyper=0.9, recovery=0.05),
        reasons=["test"],
        signal_quality=SignalQuality(
            physio=0.9, kinematics=0.9, telemetry=0.9, overall=0.9,
        ),
        timestamp=100.0,
        dwell_seconds=35.0,
    )


_VALID_PLAN_DICT: dict[str, Any] = {
    "level": "overlay_only",
    "headline": "Fix the NameError on line 10",
    "situation_summary": "1 error in main.py",
    "primary_focus": "main.py:10",
    "micro_steps": ["Read the NameError", "Define x before use"],
    "hide_targets": ["editor_symbols_except_current_function"],
    "ui_plan": {
        "dim_background": False,
        "show_overlay": True,
        "fold_unrelated_code": True,
        "intervention_type": "overlay_only",
    },
    "tone": "supportive",
    "suggested_actions": [],
    "causal_explanation": "1 active error pulled focus off the function.",
}


def _stub_response(
    input_tokens: int = 1000,
    output_tokens: int = 500,
) -> MagicMock:
    block = SimpleNamespace(
        type="tool_use",
        name="emit_intervention_plan",
        input=_VALID_PLAN_DICT,
    )
    response = MagicMock()
    response.content = [block]
    response.usage = SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    return response


def _make_stub_sdk(response: MagicMock | None = None) -> MagicMock:
    sdk = MagicMock()
    sdk.messages = MagicMock()
    sdk.messages.create = AsyncMock(return_value=response or _stub_response())
    return sdk


def _make_planner(
    cost_tracker: CostTracker,
    sdk: MagicMock | None = None,
    **config_kwargs: Any,
) -> AnthropicPlanner:
    cfg = LLMConfig(
        provider="bedrock",
        bedrock=BedrockConfig(aws_region="us-east-2"),
        use_keychain=False,
        timeout_seconds=2.0,
        max_concurrent_requests=2,
        **config_kwargs,
    )
    return AnthropicPlanner(
        cfg,
        sdk=sdk or _make_stub_sdk(),
        cost_tracker=cost_tracker,
    )


# ---------------------------------------------------------------------------
# 1. Pricing table covers all three logical model tiers
# ---------------------------------------------------------------------------


def test_pricing_covers_sonnet_haiku_opus() -> None:
    # Sonnet 4.6: $3/M in, $15/M out
    sonnet = usd_cost("claude-sonnet-4-6", input_tokens=1_000_000, output_tokens=0)
    assert sonnet == pytest.approx(3.0)
    sonnet_out = usd_cost(
        "claude-sonnet-4-6", input_tokens=0, output_tokens=1_000_000,
    )
    assert sonnet_out == pytest.approx(15.0)

    # Haiku 4.5: $1/M in, $5/M out
    haiku = usd_cost("claude-haiku-4-5", input_tokens=1_000_000, output_tokens=0)
    assert haiku == pytest.approx(1.0)
    haiku_out = usd_cost(
        "claude-haiku-4-5", input_tokens=0, output_tokens=1_000_000,
    )
    assert haiku_out == pytest.approx(5.0)

    # Opus 4.7: $15/M in, $75/M out
    opus = usd_cost("claude-opus-4-7", input_tokens=1_000_000, output_tokens=0)
    assert opus == pytest.approx(15.0)
    opus_out = usd_cost(
        "claude-opus-4-7", input_tokens=0, output_tokens=1_000_000,
    )
    assert opus_out == pytest.approx(75.0)


# ---------------------------------------------------------------------------
# 2. Cost arithmetic — Bedrock inference profile resolves correctly
# ---------------------------------------------------------------------------


def test_cost_arithmetic_bedrock_profile_resolves() -> None:
    # Same numbers via the Bedrock inference profile alias.
    via_profile = usd_cost(
        "us.anthropic.claude-sonnet-4-6-v1:0",
        input_tokens=10_000,
        output_tokens=2_000,
    )
    # 10k * $3/M + 2k * $15/M = 0.03 + 0.03 = 0.06
    assert via_profile == pytest.approx(0.06)


# ---------------------------------------------------------------------------
# 3. Per-day rollover at local midnight
# ---------------------------------------------------------------------------


def test_per_day_rollover(tmp_path: Path) -> None:
    ledger = tmp_path / "cost_ledger.json"
    tracker = CostTracker(ledger, warn_usd=5.0, kill_usd=20.0)
    monday = datetime(2026, 5, 18, 23, 59, 0)
    tuesday = datetime(2026, 5, 19, 0, 0, 1)
    tracker.record("cid_one", "claude-sonnet-4-6", 10.0, now=monday)
    assert tracker.today_total_usd(now=monday) == pytest.approx(10.0)
    # New day rolls the total back to zero.
    assert tracker.today_total_usd(now=tuesday) == pytest.approx(0.0)
    tracker.record("cid_two", "claude-sonnet-4-6", 1.0, now=tuesday)
    assert tracker.today_total_usd(now=tuesday) == pytest.approx(1.0)
    assert tracker.today_total_usd(now=monday) == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# 4. Persistence survives a "restart"
# ---------------------------------------------------------------------------


def test_persistence_survives_restart(tmp_path: Path) -> None:
    ledger = tmp_path / "cost_ledger.json"
    tracker = CostTracker(ledger, warn_usd=5.0, kill_usd=20.0)
    now = datetime(2026, 5, 19, 12, 0, 0)
    tracker.record("cid_persist", "claude-haiku-4-5", 3.5, now=now)

    # Simulate a daemon restart: build a fresh tracker on the same file.
    reloaded = CostTracker(ledger, warn_usd=5.0, kill_usd=20.0)
    assert reloaded.today_total_usd(now=now) == pytest.approx(3.5)
    # The atomic_write_json contract leaves no leftover .tmp.
    assert not (tmp_path / "cost_ledger.json.tmp").exists()


# ---------------------------------------------------------------------------
# 5. WARN fires exactly once per day
# ---------------------------------------------------------------------------


def test_warn_fires_once_per_day(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    ledger = tmp_path / "cost_ledger.json"
    tracker = CostTracker(ledger, warn_usd=1.0, kill_usd=100.0)
    now = datetime(2026, 5, 19, 9, 0, 0)
    tracker.record("cid_a", "claude-sonnet-4-6", 2.0, now=now)
    caplog.clear()
    with caplog.at_level("WARNING"):
        assert tracker.check_budget(now=now) == "WARN"
        assert tracker.check_budget(now=now) == "WARN"
        assert tracker.check_budget(now=now) == "WARN"
    warns = [r for r in caplog.records if "llm.budget.warn" in r.getMessage()]
    assert len(warns) == 1

    # Next calendar day: the WARN fires once more.
    tomorrow = now + timedelta(days=1)
    tracker.record("cid_b", "claude-sonnet-4-6", 2.0, now=tomorrow)
    caplog.clear()
    with caplog.at_level("WARNING"):
        assert tracker.check_budget(now=tomorrow) == "WARN"
    warns = [r for r in caplog.records if "llm.budget.warn" in r.getMessage()]
    assert len(warns) == 1


# ---------------------------------------------------------------------------
# 6. KILL returns fallback + sets metadata flag
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_returns_fallback_with_metadata(tmp_path: Path) -> None:
    ledger = tmp_path / "cost_ledger.json"
    tracker = CostTracker(ledger, warn_usd=1.0, kill_usd=2.0)
    # Pre-spend over the kill ceiling.
    tracker.record("cid_pre", "claude-sonnet-4-6", 5.0)
    assert tracker.check_budget() == "KILL"

    sdk = _make_stub_sdk()
    planner = _make_planner(tracker, sdk=sdk)
    plan = await planner.generate_intervention_plan(
        _make_context(), _make_state(), template_name="micro_step_planner",
    )
    assert plan.metadata.get("budget_killed") is True
    # The SDK must not have been called at all.
    sdk.messages.create.assert_not_awaited()


# ---------------------------------------------------------------------------
# 7. Budget fields read from LLMConfig
# ---------------------------------------------------------------------------


def test_llm_config_exposes_budget_fields() -> None:
    cfg = LLMConfig()
    assert hasattr(cfg, "daily_cost_budget_usd")
    assert hasattr(cfg, "cost_warn_usd")
    assert cfg.daily_cost_budget_usd == pytest.approx(20.0)
    assert cfg.cost_warn_usd == pytest.approx(5.0)
    # And user-overridable via the model.
    cfg2 = LLMConfig(daily_cost_budget_usd=2.5, cost_warn_usd=1.0)
    assert cfg2.daily_cost_budget_usd == pytest.approx(2.5)
    assert cfg2.cost_warn_usd == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 8. Per-cid grouping is queryable
# ---------------------------------------------------------------------------


def test_per_cid_grouping_queryable(tmp_path: Path) -> None:
    ledger = tmp_path / "cost_ledger.json"
    tracker = CostTracker(ledger, warn_usd=5.0, kill_usd=20.0)
    now = datetime(2026, 5, 19, 12, 0, 0)
    tracker.record("cid_alpha", "claude-sonnet-4-6", 0.10, now=now)
    tracker.record("cid_alpha", "claude-sonnet-4-6", 0.05, now=now)
    tracker.record("cid_beta", "claude-haiku-4-5", 0.02, now=now)

    alpha = tracker.per_cid_today("cid_alpha", now=now)
    beta = tracker.per_cid_today("cid_beta", now=now)
    missing = tracker.per_cid_today("cid_missing", now=now)
    assert alpha == {"total_usd": pytest.approx(0.15), "calls": 2}
    assert beta == {"total_usd": pytest.approx(0.02), "calls": 1}
    assert missing == {"total_usd": pytest.approx(0.0), "calls": 0}


# ---------------------------------------------------------------------------
# 9. Successful planner call records the per-call cost
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_planner_records_cost_on_success(tmp_path: Path) -> None:
    ledger = tmp_path / "cost_ledger.json"
    tracker = CostTracker(ledger, warn_usd=5.0, kill_usd=20.0)
    sdk = _make_stub_sdk(_stub_response(input_tokens=1000, output_tokens=200))
    planner = _make_planner(tracker, sdk=sdk)

    await planner.generate_intervention_plan(
        _make_context(), _make_state(), template_name="debug_error_summary",
    )
    # debug_error_summary → deep tier → Opus 4.7
    # 1000 * $15/M + 200 * $75/M = 0.015 + 0.015 = 0.030
    expected = usd_cost(
        "us.anthropic.claude-opus-4-7-v1:0",
        input_tokens=1000,
        output_tokens=200,
    )
    assert tracker.today_total_usd() == pytest.approx(expected)


# ---------------------------------------------------------------------------
# 10. Ill-formed ledger file is treated as cold start (no crash)
# ---------------------------------------------------------------------------


def test_corrupt_ledger_starts_empty(tmp_path: Path) -> None:
    ledger = tmp_path / "cost_ledger.json"
    ledger.write_text("not valid json {", encoding="utf-8")
    tracker = CostTracker(ledger, warn_usd=5.0, kill_usd=20.0)
    assert tracker.today_total_usd() == pytest.approx(0.0)
    # Subsequent writes still succeed.
    tracker.record("cid_recover", "claude-sonnet-4-6", 0.5)
    assert tracker.today_total_usd() == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# 11. Finding-7: the ledger rolls over at LOCAL midnight (docstring contract)
# ---------------------------------------------------------------------------


def test_rollover_is_local_midnight_not_utc(tmp_path: Path) -> None:
    """The ledger key must bucket on the *local* calendar date even when
    callers pass tz-aware UTC instants (the daemon path does). Two UTC
    instants that fall on the same local day must land in the same bucket.

    We construct two UTC instants and project both into the runner's local
    zone; whatever the local offset, an instant and the same instant + a
    few seconds share a local date, while an instant on a clearly
    different local calendar day does not.
    """
    from datetime import UTC, timedelta

    ledger = tmp_path / "cost_ledger.json"
    tracker = CostTracker(ledger, warn_usd=5.0, kill_usd=20.0)

    # Pick local noon to stay far from any midnight boundary regardless of
    # the runner's timezone, then convert to a tz-aware UTC instant — the
    # shape the daemon actually records with.
    local_noon = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    utc_a = local_noon.astimezone(UTC)
    utc_b = (local_noon + timedelta(seconds=30)).astimezone(UTC)
    next_local_day = (local_noon + timedelta(days=1)).astimezone(UTC)

    tracker.record("cid", "claude-sonnet-4-6", 4.0, now=utc_a)
    tracker.record("cid", "claude-sonnet-4-6", 3.0, now=utc_b)
    # Same local day -> accumulated into one bucket.
    assert tracker.today_total_usd(now=utc_a) == pytest.approx(7.0)
    # Next local day -> a fresh, empty bucket.
    assert tracker.today_total_usd(now=next_local_day) == pytest.approx(0.0)


def test_budget_accessors_for_gateway(tmp_path: Path) -> None:
    """Finding-7 / C-GATEWAY#1: the public budget accessors the api_gateway
    cost route reads — ``kill_usd`` / ``warn_usd`` properties and the
    side-effect-free ``budget_exhausted``."""
    ledger = tmp_path / "cost_ledger.json"
    tracker = CostTracker(ledger, warn_usd=5.0, kill_usd=20.0)
    now = datetime(2026, 5, 20, 12, 0, 0)

    assert tracker.kill_usd == pytest.approx(20.0)
    assert tracker.warn_usd == pytest.approx(5.0)
    assert tracker.budget_exhausted(now=now) is False

    tracker.record("cid", "claude-sonnet-4-6", 20.0, now=now)
    assert tracker.budget_exhausted(now=now) is True
    # Side-effect-free: repeated calls don't emit duplicate KILL logs and
    # always reflect the current spend.
    assert tracker.budget_exhausted(now=now) is True
