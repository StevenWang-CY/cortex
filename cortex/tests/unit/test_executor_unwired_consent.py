"""InterventionExecutor fail-closed authority boundary tests.

Presentation-only commands pass through. Any workspace effect must first be
blocked for lack of an exact authorization; the older consent-handler check is
still defence in depth after that gate. Unit-only legacy execution requires
``_allow_unwired_consent=True`` is set.
"""

from __future__ import annotations

import pytest

from cortex.libs.schemas.intervention import (
    AdapterCommand,
    InterventionPlan,
    MicroStep,
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
    async def test_default_execution_mode_blocks_workspace_mutation(self) -> None:
        executor = InterventionExecutor()
        executor._allow_unwired_consent = True

        class _OkAdapter:
            async def execute(self, action: str, params: dict) -> bool:
                return True

        executor.register_adapter("browser", _OkAdapter())
        mutations = await executor.apply(
            _make_plan("simplified_workspace"),
            [_make_command("close_tab")],
        )

        assert len(mutations) == 1
        assert mutations[0].success is False
        assert mutations[0].reason == "execution_mode_suggest_only"

    @pytest.mark.asyncio
    async def test_mutation_plan_blocked_without_consent_check(self) -> None:
        """A plan is not itself workspace authority."""
        executor = InterventionExecutor(execution_mode="authorized")
        plan = _make_plan("simplified_workspace")
        commands = [_make_command("close_tab"), _make_command("focus_tab")]

        mutations = await executor.apply(plan, commands)

        assert len(mutations) == 2
        for m in mutations:
            assert m.success is False
            assert m.reason == "exact_authorization_required", (
                f"expected exact_authorization_required, got {m.reason!r}"
            )

    @pytest.mark.asyncio
    async def test_guided_mode_plan_blocked_without_consent_check(self) -> None:
        """guided_mode without consent_check → refused."""
        executor = InterventionExecutor(execution_mode="authorized")
        plan = _make_plan("guided_mode")
        commands = [_make_command("focus_tab")]

        mutations = await executor.apply(plan, commands)

        assert all(m.reason == "exact_authorization_required" for m in mutations)

    @pytest.mark.asyncio
    async def test_overlay_only_plan_passes_without_consent_check(self) -> None:
        """overlay_only plans are safe to execute without a consent gate."""
        executor = InterventionExecutor(execution_mode="authorized")
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
        executor = InterventionExecutor(execution_mode="authorized")
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
