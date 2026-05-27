"""I3: ``get_password_safe`` must reuse a single bounded worker thread.

Previously every call constructed a fresh ``ThreadPoolExecutor`` and
shut it down with ``wait=False`` on timeout — every stalled keychain
call leaked one OS thread. Under a wedged Keychain (TCC prompt left
unanswered) the daemon would accumulate hundreds of stuck threads in
hours.

The new path uses a module-level singleton executor with
``max_workers=1``. This test triggers 10 simulated stalls in sequence
and asserts the thread count remains bounded at 1.
"""

from __future__ import annotations

import sys
import threading
import time
import types

import pytest

from cortex.libs.utils import secrets


def _count_cortex_keyring_threads() -> int:
    return sum(
        1 for t in threading.enumerate() if t.name.startswith("cortex-keyring")
    )


_release_event = threading.Event()


def _install_stalling_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject a fake ``keyring`` module whose ``get_password`` blocks on
    a test-controlled event so the test can release stalled workers in
    teardown — avoids polluting sibling tests that share the singleton
    executor."""
    fake = types.ModuleType("keyring")

    def _stall(_service: str, _username: str) -> None:
        # Block until the test releases the event (or 5s safety cap).
        _release_event.wait(timeout=5.0)

    fake.get_password = _stall  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "keyring", fake)


def test_repeated_timeouts_do_not_leak_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """10 simulated stalls in sequence; thread count must stay ≤ 1
    (the singleton worker). With the prior leaky implementation this
    would grow linearly to ~10."""
    _release_event.clear()
    _install_stalling_keyring(monkeypatch)
    # Reset observability counter so we can assert it ticks per timeout.
    monkeypatch.setattr(secrets, "_keyring_timeouts_total", 0)

    try:
        baseline = _count_cortex_keyring_threads()
        for _ in range(10):
            result = secrets.get_password_safe(
                "cortex.test",
                "stall",
                timeout=0.05,
            )
            assert result is None

        # The first call may spawn a thread that's still asleep; subsequent
        # calls reuse the singleton pool. We tolerate a *constant* upper
        # bound, not a growing one.
        grew_by = _count_cortex_keyring_threads() - baseline
        assert grew_by <= 1, (
            f"keyring executor leaked threads: baseline={baseline}, "
            f"after 10 stalls grew by {grew_by} (expected ≤ 1)"
        )

        # I3 observability: each timeout increments the counter.
        assert secrets.get_keyring_timeouts_total() == 10
    finally:
        # Release the stalled worker so sibling tests don't inherit a
        # busy singleton executor.
        _release_event.set()
        # Briefly yield so the worker can finish before pytest moves on.
        time.sleep(0.05)
