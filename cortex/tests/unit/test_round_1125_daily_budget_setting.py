"""The Settings "Daily LLM cap" must reach the daemon's spend kill-switch.

The desktop Settings dialog sends ``daily_llm_budget_usd`` (default 1.50 USD,
"Cortex pauses Claude calls ... once the daily cap is reached. 0 disables the
cap.") in every Apply payload. ``CortexDaemon.apply_settings`` has no branch
for the key, so the cap the user chose never reaches
``config.llm.daily_cost_budget_usd`` (the COST_RESPONSE ``budget_today``) or
the planner's ``CostTracker`` kill threshold: spend keeps running to the
built-in 20 USD default.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cortex.application.clock import FakeClock
from cortex.services.llm_engine.cost_tracker import CostTracker


@pytest.fixture()
def daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    storage = tmp_path / "storage"
    storage.mkdir()
    monkeypatch.setenv("CORTEX_STORAGE__PATH", str(storage))
    from cortex.libs.config import settings as settings_mod

    settings_mod.get_config.cache_clear()
    from cortex.services.runtime_daemon import CortexDaemon

    instance = CortexDaemon(config=settings_mod.get_config())
    instance._ws_server.broadcast_settings = AsyncMock(return_value=1)
    instance._ws_server.send_message = AsyncMock(return_value=1)
    try:
        yield instance
    finally:
        settings_mod.get_config.cache_clear()


@pytest.mark.asyncio
async def test_settings_daily_cap_becomes_the_reported_budget(daemon: Any) -> None:
    assert daemon.config.llm.daily_cost_budget_usd == 20.0

    await daemon.apply_settings({"daily_llm_budget_usd": 1.5})

    assert daemon.config.llm.daily_cost_budget_usd == pytest.approx(1.5)
    response = await daemon.get_cost_response()
    assert response.budget_today == pytest.approx(1.5)


@pytest.mark.asyncio
async def test_lowering_the_cap_below_todays_spend_stops_paid_calls(
    daemon: Any, tmp_path: Path
) -> None:
    tracker = CostTracker(
        tmp_path / "cost.json", warn_usd=5.0, kill_usd=20.0, clock=FakeClock(wall_unix_ms=1)
    )
    tracker.record("cid", "model", 3.0)
    daemon._llm_client = SimpleNamespace(_cost_tracker=tracker)
    assert tracker.check_budget() == "OK"

    await daemon.apply_settings({"daily_llm_budget_usd": 1.5})

    # The planner consults ``check_budget`` before every paid call.
    assert tracker.check_budget() == "KILL"


@pytest.mark.asyncio
async def test_a_non_finite_cap_is_ignored_not_read_as_unlimited(daemon: Any) -> None:
    await daemon.apply_settings({"daily_llm_budget_usd": float("nan")})

    assert daemon.config.llm.daily_cost_budget_usd == 20.0


def test_raising_the_cap_restores_a_clamped_warning(tmp_path: Path) -> None:
    tracker = CostTracker(
        tmp_path / "cost.json", warn_usd=5.0, kill_usd=20.0, clock=FakeClock(wall_unix_ms=1)
    )
    tracker.set_kill_usd(1.5)
    assert (tracker.warn_usd, tracker.kill_usd) == (1.5, 1.5)
    tracker.set_kill_usd(30.0)
    assert (tracker.warn_usd, tracker.kill_usd) == (5.0, 30.0)
    tracker.set_kill_usd(0.0)
    assert tracker.kill_usd == 0.0
    assert tracker.budget_exhausted() is False
