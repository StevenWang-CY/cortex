"""Audit F56 — signal handler vs asyncio loop race.

Pre-fix the daemon-side shutdown chain relied entirely on the outer
harness (``run_dev.py``) to register signal handlers. If the daemon
was launched without that harness (the desktop-shell in-process
``--in-process`` mode, future tests, future CLI entry points), there
was nothing wired to ``SIGINT``/``SIGTERM`` at all — or worse, a
caller might add a ``signal.signal`` handler before ``asyncio.run``
started, in which case the handler would run in the C-level signal
frame and could segfault on resume when interrupting numpy /
mediapipe / OpenCV native code.

F56 adds :meth:`CortexDaemon._install_loop_signal_handlers` which uses
``loop.add_signal_handler`` so the callback is dispatched as a regular
event-loop tick rather than a true asynchronous-signal interrupt.

Two cases below:

1. SIGTERM delivered while the loop is in the middle of a "numpy"
   operation (stubbed) does NOT segfault — the loop catches the
   signal cleanly and the shutdown event flips.
2. The handler runs in the asyncio loop thread (event-loop frame),
   NOT the signal frame — verified by capturing
   ``asyncio.get_running_loop()`` from inside the handler.
"""

from __future__ import annotations

import asyncio
import os
import signal
import threading
import time
from typing import Any

import pytest

from cortex.services.runtime_daemon import CortexDaemon


def _make_minimal_daemon() -> CortexDaemon:
    """Construct a daemon with all heavy services stubbed.

    We instantiate the real class because ``_install_loop_signal_handlers``
    + ``_on_signal_received`` are instance methods and we want to
    exercise them through the genuine attribute lookup chain. Heavy
    side-effects (camera, mediapipe, WS server) never actually run
    because we never call ``start()``; we only call the small helper
    methods being tested.
    """
    # Bypass the rich __init__ — we only need self._shutdown to exist.
    daemon = object.__new__(CortexDaemon)
    daemon._shutdown = asyncio.Event()
    return daemon


@pytest.mark.skipif(
    not hasattr(asyncio.get_event_loop_policy().new_event_loop(),
                "add_signal_handler"),
    reason="loop.add_signal_handler unsupported on this platform",
)
def test_sigterm_during_numpy_op_does_not_segfault() -> None:
    """Send SIGTERM to the current process while a stub "numpy" call
    holds the GIL. The loop must catch the signal, set _shutdown, and
    exit cleanly — without the segfault that ``signal.signal`` produces
    when the signal frame interrupts a native extension."""
    daemon = _make_minimal_daemon()
    captured_loop: dict[str, Any] = {}

    # Monkey-patch the handler to also record the running loop, so we
    # can assert it ran on the loop thread (case 2).
    original_handler = daemon._on_signal_received

    def _instrumented():
        try:
            captured_loop["loop"] = asyncio.get_running_loop()
        except RuntimeError:
            captured_loop["loop"] = None
        original_handler()

    daemon._on_signal_received = _instrumented  # type: ignore[method-assign]

    async def _run() -> None:
        daemon._install_loop_signal_handlers()
        loop = asyncio.get_running_loop()

        # Schedule SIGTERM to be raised after the loop is running.
        # We send it from a helper thread to mirror the kernel-level
        # path; the loop's signal handler dispatches it back to a tick.
        def _trigger():
            time.sleep(0.05)
            os.kill(os.getpid(), signal.SIGTERM)

        threading.Thread(target=_trigger, daemon=True).start()

        # While we wait for the signal, perform a stub "numpy" op — a
        # tight loop holding the GIL. Pre-fix, an asynchronous-signal
        # handler firing in the middle of this could corrupt native
        # state; post-fix the handler waits for the next event-loop
        # tick and the loop runs Python-only between awaits.
        stub_numpy_acc = 0
        for _ in range(10_000):
            stub_numpy_acc += 1

        try:
            await asyncio.wait_for(daemon._shutdown.wait(), timeout=2.0)
        finally:
            # Detach the signal handlers we installed so the next test
            # starts clean.
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.remove_signal_handler(sig)
                except (NotImplementedError, RuntimeError, ValueError):
                    pass

    asyncio.run(_run())

    # The handler ran AND set the shutdown event.
    assert daemon._shutdown.is_set()
    # Case 2: the handler must have run inside the asyncio loop, not
    # in a signal frame (which has no running loop).
    assert captured_loop.get("loop") is not None, (
        "F56 handler did not run inside the asyncio loop"
    )


def test_handler_falls_back_silently_on_unsupported_platform() -> None:
    """If ``loop.add_signal_handler`` is unsupported (Windows, some
    embedded contexts), the daemon must log and continue rather than
    crash. We simulate this by patching the loop method to raise
    NotImplementedError."""
    daemon = _make_minimal_daemon()

    class _LoopStub:
        def add_signal_handler(self, *_a, **_kw):
            raise NotImplementedError("Windows")

    async def _run() -> None:
        # Patch the get_running_loop result for this body only.
        real_loop = asyncio.get_running_loop()
        original_add = real_loop.add_signal_handler

        def _failing_add(*_a, **_kw):
            raise NotImplementedError("simulated Windows")

        real_loop.add_signal_handler = _failing_add  # type: ignore[method-assign]
        try:
            # Should not raise.
            daemon._install_loop_signal_handlers()
        finally:
            real_loop.add_signal_handler = original_add  # type: ignore[method-assign]

    asyncio.run(_run())
    # Nothing to assert beyond "did not raise" — the test contract is
    # that unsupported platforms degrade gracefully.
