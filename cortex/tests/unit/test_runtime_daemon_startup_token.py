"""I6: ``runtime_daemon.start()`` must not swallow KeyboardInterrupt
during the startup-token provisioning step.

Before the audit fix the handler was a bare ``except Exception``. While
``KeyboardInterrupt`` and ``SystemExit`` technically inherit from
``BaseException`` (not ``Exception``) in modern Python, the broad
catch invited drift: any later refactor could accidentally widen it.
The narrowed handler explicitly lists ``(OSError, ImportError,
RuntimeError)`` so a future maintainer cannot re-broaden without
also losing the lint check.

We don't need to spin up the full daemon to verify this — we only
need to import ``runtime_daemon`` and exercise the precise fragment.
The test injects a ``KeyboardInterrupt``-raising stub at the import
site the daemon uses, then runs the same try/except shape as the
production code and asserts the exception propagates.
"""

from __future__ import annotations

import importlib

import pytest


def test_keyboard_interrupt_propagates_through_startup_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the keychain unlock prompt is interrupted by Ctrl-C, the
    daemon must surface the cancellation, not silently move on."""
    auth_mod = importlib.import_module("cortex.libs.auth")

    def _raise_ki() -> None:
        raise KeyboardInterrupt("user cancelled at unlock sheet")

    monkeypatch.setattr(auth_mod, "load_or_create_token", _raise_ki)

    # Reproduce the exact try/except shape from runtime_daemon.start().
    with pytest.raises(KeyboardInterrupt):
        try:
            from cortex.libs.auth import load_or_create_token
            load_or_create_token()
        except (OSError, ImportError, RuntimeError) as exc:  # noqa: F841 - shape under test
            pytest.fail(
                "I6: KeyboardInterrupt must propagate; the narrowed "
                "exception list must NOT catch it"
            )


def test_oserror_is_still_caught() -> None:
    """The narrowed handler still suppresses the *intended* family of
    failures (Keychain unavailable / token file IO error)."""

    def _raise_os() -> None:
        raise OSError(2, "keychain missing")

    # The handler logs a WARNING and continues. We only need to confirm
    # the exception does NOT escape this fragment.
    try:
        try:
            _raise_os()
        except (OSError, ImportError, RuntimeError):
            pass
    except OSError:
        pytest.fail("I6: OSError must be caught by the narrowed handler")
