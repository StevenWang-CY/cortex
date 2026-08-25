"""Guided-break controller integration tests."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from cortex.libs.schemas.session_report import BreakRecord
from cortex.services.intervention_engine.break_overlay import GuidedBreakController


class _FakeSessionReport:
    """Capture break records without involving persistent storage."""

    def __init__(self) -> None:
        self.records: list[BreakRecord] = []
        self.counter_recommended = 0
        self.counter_taken = 0

    def record_break(
        self,
        *,
        recommended: bool = False,
        taken: bool = True,
        record: BreakRecord | None = None,
    ) -> None:
        self.counter_recommended += int(recommended)
        self.counter_taken += int(taken)
        if record is not None:
            self.records.append(record)


@pytest.mark.asyncio
async def test_break_controller_natural_completion() -> None:
    report = _FakeSessionReport()
    controller = GuidedBreakController(session_report=report)

    async def ui_handler(
        duration: float,
        pattern: str,
        audio: bool,
    ) -> tuple[float, bool]:
        assert pattern == "coherent"
        assert audio is False
        return duration, True

    controller.set_ui_handler(ui_handler)
    record = await controller.start(
        duration_seconds=240,
        breathing_pattern="coherent",
        audio_cue=False,
        reason="user_requested",
    )

    assert record is not None
    assert record.pattern == "coherent"
    assert record.pre_hrv is None
    assert record.post_hrv is None
    assert record.recovery_delta is None
    assert record.completed is True
    assert report.counter_recommended == 1
    assert report.counter_taken == 1
    assert report.records == [record]


@pytest.mark.asyncio
async def test_break_controller_early_termination_preserves_record() -> None:
    report = _FakeSessionReport()
    controller = GuidedBreakController(session_report=report)

    async def ui_handler(
        _duration: float,
        _pattern: str,
        _audio: bool,
    ) -> tuple[float, bool]:
        return 80.0, False

    controller.set_ui_handler(ui_handler)
    record = await controller.start(duration_seconds=240, reason="user_requested")

    assert record is not None
    assert record.completed is False
    assert record.duration_seconds == pytest.approx(80.0)
    assert record.pattern == "box"
    assert len(report.records) == 1


@pytest.mark.asyncio
async def test_break_controller_reentrant_start_returns_none() -> None:
    report = _FakeSessionReport()
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_ui(
        duration: float,
        _pattern: str,
        _audio: bool,
    ) -> tuple[float, bool]:
        started.set()
        await release.wait()
        return duration, True

    controller = GuidedBreakController(session_report=report)
    controller.set_ui_handler(slow_ui)

    first = asyncio.create_task(controller.start(duration_seconds=60))
    await started.wait()
    assert await controller.start(duration_seconds=60) is None
    release.set()
    first_record = await first

    assert first_record is not None
    assert first_record.completed is True
    assert len(report.records) == 1


@pytest.mark.asyncio
async def test_break_controller_no_ui_handler_returns_incomplete_record() -> None:
    report = _FakeSessionReport()
    controller = GuidedBreakController(session_report=report)

    record = await controller.start(duration_seconds=1)

    assert record is not None
    assert record.pattern == "box"
    assert record.completed is False
    assert record.duration_seconds == pytest.approx(0.0)


def test_break_controller_suppresses_only_while_active() -> None:
    calls: list[Any] = []

    async def runner() -> None:
        controller = GuidedBreakController(
            session_report=_FakeSessionReport(),
            suppress_interventions=lambda active: calls.append(active),
        )

        async def ui(duration: float, _pattern: str, _audio: bool) -> tuple[float, bool]:
            return duration, True

        controller.set_ui_handler(ui)
        await controller.start(duration_seconds=30)

    asyncio.run(runner())
    assert calls == [True, False]
