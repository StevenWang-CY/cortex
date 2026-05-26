"""P0 §3.7: BiologyBreakController integration tests."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from cortex.libs.schemas.session_report import BreakRecord
from cortex.services.intervention_engine.break_overlay import (
    BiologyBreakController,
    select_pattern,
)


def test_select_pattern_by_hrv() -> None:
    assert select_pattern(None) == "box"
    assert select_pattern(20.0) == "4-7-8"
    assert select_pattern(60.0) == "coherent"
    assert select_pattern(45.0) == "box"


class _FakeSessionReport:
    """Captures the per-break record so tests can assert on it."""

    def __init__(self) -> None:
        self.records: list[BreakRecord] = []
        self.counter_recommended: int = 0
        self.counter_taken: int = 0

    def record_break(
        self,
        *,
        recommended: bool = False,
        taken: bool = True,
        record: BreakRecord | None = None,
    ) -> None:
        if recommended:
            self.counter_recommended += 1
        if taken:
            self.counter_taken += 1
        if record is not None:
            self.records.append(record)


class _FakeStressTracker:
    def __init__(self) -> None:
        self.reset_count: int = 0
        self.credit_seconds: float = 0.0

    def reset(self) -> None:
        self.reset_count += 1

    def apply_recovery_credit(self, seconds: float) -> None:
        self.credit_seconds += float(seconds)


@pytest.mark.asyncio
async def test_break_controller_natural_completion() -> None:
    """Natural completion → recovery_delta computed, tracker reset."""
    hrv_values = iter([28.0, 42.0])
    stress = _FakeStressTracker()
    report = _FakeSessionReport()
    controller = BiologyBreakController(
        hrv_sampler=lambda: next(hrv_values),
        session_report=report,
        suppress_interventions=lambda _active: None,
        stress_tracker=stress,
    )

    async def ui_handler(duration: float, _pattern: str, _audio: bool) -> tuple[float, bool]:
        # Simulate a full run.
        return duration, True

    controller.set_ui_handler(ui_handler)
    record = await controller.start(
        duration_seconds=240,
        breathing_pattern=None,  # auto-select from HRV (28 → 4-7-8)
        audio_cue=False,
        reason="stress_integral_crossed_threshold",
    )
    assert record is not None
    assert record.pattern == "4-7-8"
    assert record.pre_hrv == 28.0
    assert record.post_hrv == 42.0
    assert record.recovery_delta == pytest.approx(14.0)
    assert record.completed is True
    assert stress.reset_count == 1
    assert stress.credit_seconds == 0.0
    assert len(report.records) == 1
    assert report.records[0] is record


@pytest.mark.asyncio
async def test_break_controller_early_termination_preserves_record() -> None:
    """Early termination → record preserved with completed=False + credit applied."""
    hrv_values = iter([28.0, 30.0])
    stress = _FakeStressTracker()
    report = _FakeSessionReport()
    controller = BiologyBreakController(
        hrv_sampler=lambda: next(hrv_values),
        session_report=report,
        suppress_interventions=lambda _active: None,
        stress_tracker=stress,
    )

    async def ui_handler(_duration: float, _pattern: str, _audio: bool) -> tuple[float, bool]:
        # User ended after 80s of a 240s break.
        return 80.0, False

    controller.set_ui_handler(ui_handler)
    record = await controller.start(
        duration_seconds=240,
        breathing_pattern="box",
        audio_cue=True,
        reason="user_requested",
    )
    assert record is not None
    assert record.completed is False
    assert record.duration_seconds == pytest.approx(80.0)
    assert stress.reset_count == 0
    assert stress.credit_seconds == pytest.approx(80.0)
    assert len(report.records) == 1


@pytest.mark.asyncio
async def test_break_controller_reentrant_start_returns_none() -> None:
    """A second concurrent start() while one is in flight is a no-op."""
    stress = _FakeStressTracker()
    report = _FakeSessionReport()
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_ui(duration: float, _pattern: str, _audio: bool) -> tuple[float, bool]:
        started.set()
        await release.wait()
        return duration, True

    controller = BiologyBreakController(
        hrv_sampler=lambda: 50.0,
        session_report=report,
        suppress_interventions=lambda _active: None,
        stress_tracker=stress,
    )
    controller.set_ui_handler(slow_ui)

    first = asyncio.create_task(controller.start(duration_seconds=60))
    await started.wait()
    second = await controller.start(duration_seconds=60)
    assert second is None
    release.set()
    first_record = await first
    assert first_record is not None
    assert first_record.completed is True


@pytest.mark.asyncio
async def test_break_controller_runs_without_ui_handler() -> None:
    """When no UI is bound the controller still produces a valid record."""
    stress = _FakeStressTracker()
    report = _FakeSessionReport()
    controller = BiologyBreakController(
        hrv_sampler=lambda: 50.0,
        session_report=report,
        suppress_interventions=lambda _active: None,
        stress_tracker=stress,
    )
    # No set_ui_handler call → falls back to asyncio.sleep(duration).
    # Set duration to 0.01 to keep the test fast.
    record = await controller.start(duration_seconds=30)
    assert record is not None
    assert record.pattern == "box"  # 50 ms → middle of the band


def test_break_controller_suppress_callback() -> None:
    """suppress_interventions is called True/False around the break."""
    calls: list[Any] = []

    async def runner() -> None:
        controller = BiologyBreakController(
            hrv_sampler=lambda: 50.0,
            session_report=_FakeSessionReport(),
            suppress_interventions=lambda active: calls.append(active),
            stress_tracker=_FakeStressTracker(),
        )

        async def ui(d: float, _p: str, _a: bool) -> tuple[float, bool]:
            return d, True
        controller.set_ui_handler(ui)
        await controller.start(duration_seconds=30)

    asyncio.run(runner())
    assert calls == [True, False]
