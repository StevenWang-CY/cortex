"""Audit-2 — ``_trigger_special_intervention`` double-spawn race.

Two state-loop ticks landing in the same scheduling window used to both
pass the ``if self._active_intervention_id is not None`` guard because
the sentinel was set *after* the LLM await. The fix sets
``__pending__`` synchronously before the await; this test pins it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from cortex.services.runtime_daemon import CortexDaemon


class _RecordingClient:
    """Stand-in LLM client that counts how many times generate_intervention_plan
    is called and returns a stable plan-shaped object."""

    def __init__(self, latency: float = 0.05) -> None:
        self.calls = 0
        self._latency = latency

    async def generate_intervention_plan(
        self, *_args: Any, **kwargs: Any
    ) -> Any:
        self.calls += 1
        await asyncio.sleep(self._latency)
        return SimpleNamespace(
            intervention_id=f"iv-{self.calls}",
            level="overlay_only",
            headline="x",
            situation_summary="",
            primary_focus="",
            micro_steps=["a"],
            hide_targets=[],
            ui_plan=SimpleNamespace(model_dump=lambda: {}),
            tone="neutral",
            suggested_actions=[],
            error_analysis=None,
            tab_recommendations=None,
            causal_explanation=None,
            consent_level="suggest",
            plan_warnings=[],
            metadata={},
            model_dump=lambda mode="json": {"intervention_id": f"iv-{self.calls}"},
        )


@pytest.fixture
def daemon(monkeypatch: pytest.MonkeyPatch) -> CortexDaemon:
    d = CortexDaemon.__new__(CortexDaemon)
    d._active_intervention_id = None
    d._last_policy_decision_id = None
    d._last_policy_arm = None
    d._last_policy_propensity = None
    d._llm_client = _RecordingClient()

    # Patch enrich_plan_with_context inside runtime_daemon's namespace.
    import cortex.services.runtime_daemon as rd

    monkeypatch.setattr(rd, "enrich_plan_with_context", lambda plan, ctx: plan)

    d._recorder = SimpleNamespace(append=lambda *a, **k: None)

    class _WS:
        async def send_message(self, *_a: Any, **_k: Any) -> int:
            return 1

    d._ws_server = _WS()
    return d


def test_double_spawn_results_in_single_llm_call(daemon: CortexDaemon) -> None:
    """Schedule two _trigger_special_intervention coroutines back-to-back;
    only one must actually invoke generate_intervention_plan because the
    sentinel rejects the second call's guard immediately."""

    async def runner() -> None:
        t1 = asyncio.create_task(
            daemon._trigger_special_intervention(
                context=SimpleNamespace(),
                estimate=SimpleNamespace(),
                template_name="breathing",
            )
        )
        # Spawn the second WITHOUT yielding to t1. The first task's
        # sentinel assignment runs synchronously up to the first await.
        # The second task should observe ``__pending__`` and bail.
        t2 = asyncio.create_task(
            daemon._trigger_special_intervention(
                context=SimpleNamespace(),
                estimate=SimpleNamespace(),
                template_name="active_recall",
            )
        )
        await asyncio.gather(t1, t2)

    asyncio.run(runner())
    assert daemon._llm_client.calls == 1, (
        f"Expected 1 LLM call, got {daemon._llm_client.calls} "
        "(__pending__ sentinel race regressed)"
    )


def test_finally_clears_sentinel_on_failure(daemon: CortexDaemon) -> None:
    """If the LLM call raises, the finally block must clear the sentinel
    so subsequent ticks can fire interventions again."""

    async def _raise(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("synthetic failure")

    daemon._llm_client.generate_intervention_plan = _raise  # type: ignore[assignment]

    asyncio.run(
        daemon._trigger_special_intervention(
            context=SimpleNamespace(),
            estimate=SimpleNamespace(),
            template_name="breathing",
        )
    )
    assert daemon._active_intervention_id is None
