"""P0 §3.4 — unit tests for CalibrationRunner.

The runner wraps the existing simulate + live calibration loops so the
desktop shell wizard, the Settings recalibrate button, and the
developer CLI all drive the same code path. These tests cover only the
simulate path so they run on CI without OpenCV / a webcam.

Cases:

1. ``test_simulate_runner_writes_baselines`` — a short simulated run
   ends with ``storage/baselines/default.json`` present and parseable
   as a :class:`UserBaselines`.
2. ``test_abort_releases_camera`` — calling ``abort()`` mid-run causes
   ``start()`` to return promptly without raising; ``finish()`` raises
   because we never overwrite ``default.json`` with a partial run.
3. ``test_progress_callback_fired`` — the callback fires at least N
   times during a 3-second simulate.
4. ``test_finish_before_start_raises`` — calling ``finish()`` before
   ``start()`` is a defensive error.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from cortex.libs.schemas.state import UserBaselines
from cortex.services.capture_service.calibration_runner import (
    CalibrationProgress,
    CalibrationRunner,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture()
def baselines_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``storage.path`` to a tmp dir so the runner's atomic
    write doesn't pollute the repo's checked-in baseline file."""
    from cortex.libs.config import settings as config_module

    config = config_module.get_config()
    monkeypatch.setattr(config.storage, "path", str(tmp_path))
    return tmp_path / "baselines"


def test_simulate_runner_writes_baselines(baselines_dir: Path) -> None:
    runner = CalibrationRunner(duration_seconds=2, simulate=True)
    asyncio.run(runner.start())
    baselines = asyncio.run(runner.finish())

    assert isinstance(baselines, UserBaselines)
    default_path = baselines_dir / "default.json"
    assert default_path.exists(), "default.json must be written on success"

    # Round-trip through the schema.
    payload = json.loads(default_path.read_text())
    reloaded = UserBaselines.model_validate(payload)
    assert reloaded.hr_baseline > 0
    assert reloaded.hrv_baseline > 0
    # Timestamped sibling is also present.
    siblings = list(baselines_dir.glob("baseline_*.json"))
    assert siblings, "timestamped baseline file must be present"


def test_abort_releases_camera(baselines_dir: Path) -> None:
    """Aborting mid-run returns cleanly; finish() refuses to write a
    partial baseline so we never overwrite the prior known-good file."""
    runner = CalibrationRunner(duration_seconds=10, simulate=True)

    async def _drive() -> None:
        async def _abort_soon() -> None:
            await asyncio.sleep(0.2)
            runner.abort()
        await asyncio.gather(runner.start(), _abort_soon())

    asyncio.run(_drive())

    # finish() must refuse to overwrite default.json with a partial run.
    with pytest.raises(RuntimeError):
        asyncio.run(runner.finish())

    # default.json must NOT exist — we never crossed the success gate.
    assert not (baselines_dir / "default.json").exists()


def test_progress_callback_fired(baselines_dir: Path) -> None:
    """Over a 3-second simulate the callback should fire at least
    several times (the loop runs at ~2 Hz, so >= 4 ticks)."""
    received: list[CalibrationProgress] = []

    def _cb(progress: CalibrationProgress) -> None:
        received.append(progress)

    runner = CalibrationRunner(duration_seconds=3, simulate=True)
    asyncio.run(runner.start(on_progress=_cb))

    assert len(received) >= 4, (
        f"expected >= 4 progress callbacks, got {len(received)}"
    )
    # The final emission should be the completion sentinel.
    statuses = {p.status for p in received}
    assert "completed" in statuses
    # pct_complete should monotonically increase across emissions.
    pcts = [p.pct_complete for p in received if p.status == "running"]
    assert pcts == sorted(pcts)


def test_finish_before_start_raises(baselines_dir: Path) -> None:
    runner = CalibrationRunner(duration_seconds=2, simulate=True)
    with pytest.raises(RuntimeError):
        asyncio.run(runner.finish())


def test_start_twice_raises(baselines_dir: Path) -> None:
    """Defensive: calling start() twice is a programming error."""
    runner = CalibrationRunner(duration_seconds=1, simulate=True)
    asyncio.run(runner.start())
    with pytest.raises(RuntimeError):
        asyncio.run(runner.start())


def test_negative_duration_raises() -> None:
    with pytest.raises(ValueError):
        CalibrationRunner(duration_seconds=0, simulate=True)


# ---------------------------------------------------------------------------
# P1 — calibration must NOT silently calibrate against synthetic frames when
# the live camera is unavailable/contended. ``used_simulation`` lets the
# desktop wizard surface a visible warning instead of accepting a useless
# baseline (CLAUDE.md rules #5/#15).
# ---------------------------------------------------------------------------


def test_simulate_runner_marks_used_simulation() -> None:
    from cortex.services.capture_service.calibration_runner import CalibrationRunner

    runner = CalibrationRunner(duration_seconds=1, simulate=True)
    assert runner.used_simulation is True


def test_live_calibration_invokes_on_fallback_when_camera_unavailable(
    monkeypatch: pytest.MonkeyPatch, baselines_dir: Path
) -> None:
    import cortex.services.capture_service.webcam as webcam_mod
    from cortex.services.capture_service.calibration_runner import run_live_calibration

    # Camera cannot be opened -> live path must degrade to simulation AND
    # signal the fallback so the caller can warn the user.
    monkeypatch.setattr(webcam_mod, "open_video_capture", lambda cfg: (None, None))
    fired = {"fallback": False}

    asyncio.run(
        run_live_calibration(
            1,
            is_aborted=lambda: True,
            on_fallback=lambda: fired.__setitem__("fallback", True),
        )
    )
    assert fired["fallback"] is True


def test_runner_marks_used_simulation_on_camera_fallback(
    monkeypatch: pytest.MonkeyPatch, baselines_dir: Path
) -> None:
    import cortex.services.capture_service.webcam as webcam_mod
    from cortex.services.capture_service.calibration_runner import CalibrationRunner

    monkeypatch.setattr(webcam_mod, "open_video_capture", lambda cfg: (None, None))
    runner = CalibrationRunner(duration_seconds=1, simulate=False)
    assert runner.used_simulation is False
    runner.abort()  # exit the simulate loop immediately
    asyncio.run(runner.start())
    assert runner.used_simulation is True
