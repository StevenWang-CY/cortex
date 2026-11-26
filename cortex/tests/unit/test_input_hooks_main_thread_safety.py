"""The keyboard listener must not touch HIToolbox from its own thread.

Found on the packaged 0.6.0 build (macOS 26): ending a session and starting
another crashed the app with SIGTRAP in ``dispatch_assert_queue`` under
``TSMGetInputSourceProperty``, called by pynput's ``keycode_context`` on the
keyboard listener thread.
"""

from __future__ import annotations

import inspect
import sys

import pytest

pynput_keyboard = pytest.importorskip("pynput.keyboard")

from cortex.services.telemetry_engine.input_hooks import (  # noqa: E402
    InputHooks,
    keyboard_listener_class,
)

darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="macOS listener backend")


@darwin_only
def test_listener_thread_never_reads_the_keyboard_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    from pynput._util import darwin as pynput_darwin
    from pynput.keyboard import _darwin as keyboard_darwin

    def _forbidden() -> None:
        raise AssertionError("keycode_context must not run on the listener thread")

    monkeypatch.setattr(keyboard_darwin, "keycode_context", _forbidden)
    ran: list[object] = []
    monkeypatch.setattr(
        pynput_darwin.ListenerMixin, "_run", lambda self: ran.append(self), raising=True
    )

    listener = keyboard_listener_class()(on_press=None, on_release=None)
    listener._run()

    assert ran == [listener]


@darwin_only
def test_listening_path_does_not_depend_on_the_skipped_layout() -> None:
    """If a pynput upgrade starts translating keys through the layout while
    listening, skipping the layout read is no longer safe and this must fail."""

    from pynput.keyboard import _darwin as keyboard_darwin

    listener = keyboard_darwin.Listener
    for method in ("_event_to_key", "_handle_message"):
        source = inspect.getsource(getattr(listener, method))
        assert "_context" not in source, method
        assert "keycode_to_string" not in source, method


@darwin_only
def test_safe_listener_is_still_the_library_listener() -> None:
    cls = keyboard_listener_class()
    assert issubclass(cls, pynput_keyboard.Listener)
    assert cls is not pynput_keyboard.Listener


def test_input_hooks_build_the_keyboard_listener_through_the_safe_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``InputHooks.start`` must not construct ``keyboard.Listener`` directly."""

    from cortex.services.telemetry_engine import input_hooks as module

    built: list[str] = []

    class _FakeListener:
        IS_TRUSTED = True

        def __init__(self, **_callbacks: object) -> None:
            built.append(type(self).__name__)

        def start(self) -> None:
            return None

        def stop(self) -> None:
            return None

        def is_alive(self) -> bool:
            return True

        def join(self, timeout: float | None = None) -> None:
            return None

    class _FakeKeyboard(_FakeListener):
        pass

    class _FakeMouse(_FakeListener):
        pass

    monkeypatch.setattr(module, "keyboard_listener_class", lambda: _FakeKeyboard)
    import pynput.mouse

    monkeypatch.setattr(pynput.mouse, "Listener", _FakeMouse)
    monkeypatch.setattr(
        pynput_keyboard,
        "Listener",
        lambda **_kw: (_ for _ in ()).throw(AssertionError("unsafe listener constructed")),
    )

    hooks = InputHooks()
    try:
        assert hooks.start() is True
        assert sorted(built) == ["_FakeKeyboard", "_FakeMouse"]
    finally:
        hooks.stop()
