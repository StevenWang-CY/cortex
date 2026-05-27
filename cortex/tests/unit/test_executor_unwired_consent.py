"""P1-7: InterventionExecutor must default-deny mutation plans when consent_check is not wired.

overlay_only plans pass through (no mutation required). Any other plan
level (simplified_workspace, guided_mode) must be blocked with
reason="consent_handler_not_wired" unless the escape hatch
``_allow_unwired_consent=True`` is set.
"""

from __future__ import annotations

import pytest

from cortex.libs.schemas.intervention import (
    AdapterCommand,
    InterventionPlan,
    MicroStep,
    SuggestedAction,
    UIPlan,
)
from cortex.services.intervention_engine.executor import InterventionExecutor


def _make_plan(level: str) -> InterventionPlan:
    return InterventionPlan(
        level=level,  # type: ignore[arg-type]
        situation_summary="Test plan for consent gate.",
        headline="Focus on one thing",
        primary_focus="The current file",
        micro_steps=[MicroStep(text="Close extra tabs")],
        ui_plan=UIPlan(),
    )


def _make_command(action: str = "close_tab") -> AdapterCommand:
    return AdapterCommand(adapter="browser", action=action, params={})


class TestExecutorUnwiredConsent:
    @pytest.mark.asyncio
    async def test_mutation_plan_blocked_without_consent_check(self) -> None:
        """simplified_workspace without consent_check → all mutations refused."""
        executor = InterventionExecutor()
        plan = _make_plan("simplified_workspace")
        commands = [_make_command("close_tab"), _make_command("focus_tab")]

        mutations = await executor.apply(plan, commands)

        assert len(mutations) == 2
        for m in mutations:
            assert m.success is False
            assert m.reason == "consent_handler_not_wired", (
                f"expected consent_handler_not_wired, got {m.reason!r}"
            )

    @pytest.mark.asyncio
    async def test_guided_mode_plan_blocked_without_consent_check(self) -> None:
        """guided_mode without consent_check → refused."""
        executor = InterventionExecutor()
        plan = _make_plan("guided_mode")
        commands = [_make_command("focus_tab")]

        mutations = await executor.apply(plan, commands)

        assert all(m.reason == "consent_handler_not_wired" for m in mutations)

    @pytest.mark.asyncio
    async def test_overlay_only_plan_passes_without_consent_check(self) -> None:
        """overlay_only plans are safe to execute without a consent gate."""
        executor = InterventionExecutor()
        # Register a no-op adapter so the command can succeed
        class _OkAdapter:
            async def execute(self, action: str, params: dict) -> bool:
                return True

        executor.register_adapter("overlay", _OkAdapter())
        plan = _make_plan("overlay_only")
        commands = [AdapterCommand(adapter="overlay", action="show_overlay", params={})]

        mutations = await executor.apply(plan, commands)

        assert len(mutations) == 1
        assert mutations[0].success is True

    @pytest.mark.asyncio
    async def test_escape_hatch_allows_mutation_without_consent_check(self) -> None:
        """When _allow_unwired_consent=True the default-deny is bypassed."""
        executor = InterventionExecutor()
        executor._allow_unwired_consent = True  # test escape hatch

        class _OkAdapter:
            async def execute(self, action: str, params: dict) -> bool:
                return True

        executor.register_adapter("browser", _OkAdapter())
        plan = _make_plan("simplified_workspace")
        commands = [_make_command("focus_tab")]

        mutations = await executor.apply(plan, commands)

        assert len(mutations) == 1
        assert mutations[0].success is True
