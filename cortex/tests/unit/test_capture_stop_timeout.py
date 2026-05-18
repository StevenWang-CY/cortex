"""Audit F01 — capture pipeline ``stop()`` is bounded by a timeout.

Previously, a disconnected USB webcam or stuck mediapipe worker could
block ``CapturePipeline.stop()`` forever, hanging the daemon's
``RuntimeDaemon.stop()`` chain. The only escape was SIGKILL, which
defeats the very kill chain it was meant to support — and on macOS the
AVFoundation camera handle stays owned by the dead PID for ~minutes.

This test exercises the new ``asyncio.wait_for(... timeout=5.0)``
wrapper directly: a stub pipeline whose ``stop()`` never completes
demonstrates that the bound fires on schedule.
"""

from __future__ import annotations

import asyncio
import time

import pytest


class _NeverFinishingPipeline:
    async def stop(self) -> None:
        # Simulate a stuck USB-camera close — never returns.
        await asyncio.sleep(60)


@pytest.mark.asyncio
async def test_wait_for_bounds_a_hung_capture_stop() -> None:
    """The exact wrapper pattern used in runtime_daemon.stop(): a hung
    capture pipeline must surface as ``TimeoutError`` within ~5s."""
    pipeline = _NeverFinishingPipeline()
    t0 = time.monotonic()
    # Use a short timeout for test speed; the production value is 5.0.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(pipeline.stop(), timeout=0.25)
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, f"timeout took too long: {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_wait_for_passes_through_quick_stop() -> None:
    """A normal pipeline that returns quickly must not be interrupted."""

    class _FastPipeline:
        called = False

        async def stop(self) -> None:
            type(self).called = True

    pipeline = _FastPipeline()
    await asyncio.wait_for(pipeline.stop(), timeout=1.0)
    assert pipeline.called


@pytest.mark.asyncio
async def test_wait_for_propagates_non_timeout_errors() -> None:
    """A pipeline that raises during stop() must propagate the real
    error; the timeout wrapper does not swallow it."""

    class _FailingPipeline:
        async def stop(self) -> None:
            raise RuntimeError("camera bus error")

    pipeline = _FailingPipeline()
    with pytest.raises(RuntimeError, match="camera bus error"):
        await asyncio.wait_for(pipeline.stop(), timeout=1.0)
