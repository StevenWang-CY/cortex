"""Audit P1 — shutdown guard for the macOS UN delegate.

When the daemon tears down it MUST flip a module-level flag that any
in-flight Cocoa notification callback honours BEFORE invoking the
registered handler — otherwise a user click that arrives during
``daemon.stop()`` reaches a half-torn-down daemon (cancelled asyncio
tasks, closed loop) and can crash the process.

The module is import-safe on non-mac / when PyObjC is missing, so the
guard semantics are tested against the public surface; the PyObjC-
dependent delegate construction is covered by a separate smoke test
that skips on Linux.
"""

from __future__ import annotations

import sys
import threading

import pytest

from cortex.libs.utils import macos_notifications as mn


@pytest.fixture(autouse=True)
def _reset_module_state() -> None:
    """Each test starts with a clean shutdown latch + handler."""
    mn.reset_shutdown_state_for_tests()
    mn.set_user_action_handler(None)
    yield
    mn.reset_shutdown_state_for_tests()
    mn.set_user_action_handler(None)


def test_shutdown_latch_starts_unset() -> None:
    assert mn.is_shutting_down() is False


def test_mark_shutting_down_flips_latch() -> None:
    mn.mark_shutting_down()
    assert mn.is_shutting_down() is True


def test_mark_shutting_down_is_idempotent() -> None:
    mn.mark_shutting_down()
    mn.mark_shutting_down()  # second call must not raise
    assert mn.is_shutting_down() is True


def test_reset_helper_clears_latch() -> None:
    mn.mark_shutting_down()
    mn.reset_shutdown_state_for_tests()
    assert mn.is_shutting_down() is False


def test_latch_is_thread_safe() -> None:
    """Cocoa callbacks fire on the AppKit main thread; daemon shutdown
    runs on the asyncio thread. The latch must be observable across
    threads without explicit locking on the caller side. ``threading.
    Event`` provides that guarantee; this test just exercises it.
    """
    seen: list[bool] = []
    started = threading.Event()

    def _observer() -> None:
        started.set()
        # Spin briefly until the writer sets the latch.
        for _ in range(1000):
            if mn.is_shutting_down():
                seen.append(True)
                return
            # Yield to other threads.
            threading.Event().wait(0.001)
        seen.append(False)

    t = threading.Thread(target=_observer)
    t.start()
    started.wait(timeout=2.0)
    mn.mark_shutting_down()
    t.join(timeout=2.0)
    assert seen == [True]


def test_handler_remains_registered_across_shutdown_signal() -> None:
    """``mark_shutting_down()`` does not touch the registered handler
    — it only flips the guard the delegate consults. This keeps the
    state machine simple and lets unit tests re-arm between runs.
    """
    calls: list[tuple[str, str]] = []

    def _handler(iv_id: str, action_id: str) -> None:
        calls.append((iv_id, action_id))

    mn.set_user_action_handler(_handler)
    mn.mark_shutting_down()
    # Module-private read for the test only — verifies the handler is
    # still on file. The delegate itself will refuse to call it while
    # the latch is set.
    assert mn._user_action_handler is _handler


@pytest.mark.skipif(sys.platform != "darwin", reason="PyObjC required")
def test_delegate_short_circuits_during_shutdown() -> None:
    """On macOS we can construct the real delegate and verify its
    callback path honours the shutdown guard: the completion handler
    still fires (OS contract) but the user-action handler MUST NOT be
    invoked.
    """
    un = mn._load_user_notifications()
    if un is None:
        pytest.skip("UserNotifications framework unavailable")
    delegate = mn._build_delegate(un)
    if delegate is None:
        pytest.skip("delegate construction returned None")

    invocations: list[tuple[str, str]] = []

    def _handler(iv_id: str, action_id: str) -> None:
        invocations.append((iv_id, action_id))

    mn.set_user_action_handler(_handler)

    completion_called: list[bool] = []

    def _completion() -> None:
        completion_called.append(True)

    # Build fakes shaped like the Cocoa response objects the callback
    # touches: ``response.actionIdentifier()``, ``response.notification
    # ().request().identifier()``.
    class _Request:
        def identifier(self) -> str:
            return "cortex_intervention_iv_abc"

    class _Notification:
        def request(self) -> _Request:
            return _Request()

    class _Response:
        def actionIdentifier(self) -> str:
            return mn._ACTION_OPEN

        def notification(self) -> _Notification:
            return _Notification()

    # Sanity: handler IS invoked when the guard is clear.
    delegate.userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(
        None, _Response(), _completion,
    )
    assert invocations == [("iv_abc", "open")]
    assert completion_called == [True]

    # Now flip the guard and verify the handler is skipped while the
    # completion handler still fires (OS contract).
    invocations.clear()
    completion_called.clear()
    mn.mark_shutting_down()
    delegate.userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(
        None, _Response(), _completion,
    )
    assert invocations == []
    assert completion_called == [True]


@pytest.mark.skipif(sys.platform != "darwin", reason="PyObjC required")
def test_will_present_suppressed_during_shutdown() -> None:
    """The ``willPresent`` callback chooses presentation options for a
    foreground delivery. During shutdown it must reply with ``0``
    (UNNotificationPresentationOptionNone) so the OS doesn't show a
    banner whose click would route into a dead daemon.
    """
    un = mn._load_user_notifications()
    if un is None:
        pytest.skip("UserNotifications framework unavailable")
    delegate = mn._build_delegate(un)
    if delegate is None:
        pytest.skip("delegate construction returned None")

    received_options: list[int] = []

    def _completion(opts: int) -> None:
        received_options.append(int(opts))

    # Clear guard: full presentation (banner + list + sound = 7).
    delegate.userNotificationCenter_willPresentNotification_withCompletionHandler_(
        None, None, _completion,
    )
    assert received_options == [7]

    # Shutdown: no presentation.
    received_options.clear()
    mn.mark_shutting_down()
    delegate.userNotificationCenter_willPresentNotification_withCompletionHandler_(
        None, None, _completion,
    )
    assert received_options == [0]
