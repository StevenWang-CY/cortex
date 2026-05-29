"""Phase-4a Debt-1: :func:`cortex.libs.utils.secrets.get_password_safe`
timeout regression tests.

The helper wraps ``keyring.get_password`` in a thread pool so a stalled
backend (macOS unlock sheet, dbus stall) cannot pin the calling event
loop indefinitely. These tests exercise the three branches:

1. A backend that returns promptly produces the expected value.
2. A backend that blocks longer than the timeout returns ``None`` and
   logs a warning (rather than hanging the test forever).
3. A backend that raises ``RuntimeError`` (broken keyring on Linux)
   returns ``None`` silently — caller falls back to env.
"""

from __future__ import annotations

import sys
import threading
import time
import types

from cortex.libs.utils import secrets as secrets_mod


def _install_fake_keyring(get_password) -> None:
    """Install a fake ``keyring`` module that delegates to the supplied
    callable. Removed by the surrounding ``try / finally`` so tests
    don't leak side effects into siblings."""
    fake = types.ModuleType("keyring")
    fake.get_password = get_password  # type: ignore[attr-defined]
    sys.modules["keyring"] = fake


def _remove_fake_keyring() -> None:
    sys.modules.pop("keyring", None)


def test_get_password_safe_returns_value_on_prompt_backend() -> None:
    """Fast happy-path: backend returns synchronously, helper passes the
    value through unchanged."""

    def fake_get_password(service: str, username: str) -> str:
        assert service == "svc"
        assert username == "acct"
        return "TOKEN"

    _install_fake_keyring(fake_get_password)
    try:
        result = secrets_mod.get_password_safe("svc", "acct", timeout=2.0)
    finally:
        _remove_fake_keyring()
    assert result == "TOKEN"


def test_get_password_safe_returns_none_on_timeout() -> None:
    """A blocking backend must not pin the helper — after ``timeout``
    seconds the helper returns None and never raises into the caller."""
    started = threading.Event()

    def stalling_get_password(service: str, username: str) -> str | None:
        started.set()
        # Block for longer than the helper's timeout. The thread leaks
        # — see helper docstring — but the helper releases the caller.
        time.sleep(2.0)
        return "should not be seen"

    _install_fake_keyring(stalling_get_password)
    try:
        t0 = time.monotonic()
        result = secrets_mod.get_password_safe("svc", "acct", timeout=0.25)
        elapsed = time.monotonic() - t0
    finally:
        _remove_fake_keyring()

    assert result is None
    # The helper must return within roughly the timeout (small slop for
    # CI runners). It must NOT have blocked the full 2 s the backend
    # would take.
    assert elapsed < 1.5, f"helper blocked {elapsed:.2f}s past timeout"
    assert started.wait(timeout=1.0), "backend thread never started"


def test_get_password_safe_timeout_increments_prometheus_counter() -> None:
    """C6 (audit): the keyring read-timeout branch must bump the
    Prometheus ``KEYRING_TIMEOUTS_TOTAL`` counter (it shipped a permanent
    zero before the producer was wired). Asserts the delta is exactly +1
    across one timed-out call."""
    from cortex.libs.observability.metrics import KEYRING_TIMEOUTS_TOTAL

    def stalling_get_password(service: str, username: str) -> str | None:
        time.sleep(2.0)
        return "unused"

    before = KEYRING_TIMEOUTS_TOTAL._value.get()
    _install_fake_keyring(stalling_get_password)
    try:
        result = secrets_mod.get_password_safe("svc", "acct", timeout=0.25)
    finally:
        _remove_fake_keyring()
    after = KEYRING_TIMEOUTS_TOTAL._value.get()

    assert result is None
    assert after - before == 1.0, (
        f"expected KEYRING_TIMEOUTS_TOTAL +1 on timeout; "
        f"before={before} after={after}"
    )


def test_set_password_safe_timeout_increments_prometheus_counter() -> None:
    """C6 (audit): the keyring WRITE-timeout branch counts on the same
    Prometheus counter so /metrics reflects every stalled Keychain call,
    not just reads."""
    from cortex.libs.observability.metrics import KEYRING_TIMEOUTS_TOTAL

    def stalling_set_password(service: str, account: str, password: str) -> None:
        time.sleep(2.0)

    fake = types.ModuleType("keyring")
    fake.set_password = stalling_set_password  # type: ignore[attr-defined]
    sys.modules["keyring"] = fake

    before = KEYRING_TIMEOUTS_TOTAL._value.get()
    try:
        ok = secrets_mod.set_password_safe("svc", "acct", "secret", timeout=0.25)
    finally:
        _remove_fake_keyring()
    after = KEYRING_TIMEOUTS_TOTAL._value.get()

    assert ok is False
    assert after - before == 1.0, (
        f"expected KEYRING_TIMEOUTS_TOTAL +1 on write timeout; "
        f"before={before} after={after}"
    )


def test_get_password_safe_returns_none_on_backend_exception() -> None:
    """A broken keyring backend (no implementation registered) raises
    ``RuntimeError`` / a custom keyring error. The helper must swallow
    and return None so the caller falls back to env vars."""

    def broken_get_password(service: str, username: str) -> str | None:
        raise RuntimeError("No suitable backend")

    _install_fake_keyring(broken_get_password)
    try:
        result = secrets_mod.get_password_safe("svc", "acct", timeout=1.0)
    finally:
        _remove_fake_keyring()
    assert result is None


def test_get_password_safe_returns_none_when_keyring_absent() -> None:
    """When the ``keyring`` package isn't installed at all, the helper
    must degrade silently to None rather than crashing the import."""
    # Force ImportError by deleting any existing module.
    _remove_fake_keyring()
    # Also block the import path so the helper's lazy import fails.
    sys.modules.pop("keyring", None)
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def fake_import(name, *args, **kwargs):
        if name == "keyring":
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    if isinstance(__builtins__, dict):
        __builtins__["__import__"] = fake_import  # type: ignore[index]
    else:
        __builtins__.__import__ = fake_import  # type: ignore[attr-defined]
    try:
        result = secrets_mod.get_password_safe("svc", "acct", timeout=0.5)
    finally:
        if isinstance(__builtins__, dict):
            __builtins__["__import__"] = real_import  # type: ignore[index]
        else:
            __builtins__.__import__ = real_import  # type: ignore[attr-defined]
    assert result is None
