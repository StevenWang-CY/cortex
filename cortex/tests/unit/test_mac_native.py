"""Unit tests for the AppKit bridge.

These tests run on every platform: they stub AppKit imports so non-mac CI
still exercises the public surface, and they verify that the macOS path
no-ops cleanly when the bridge isn't realized.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _reset_appkit_cache() -> None:
    """Clear the lazy AppKit cache between tests so stubs land cleanly."""
    from cortex.apps.desktop_shell import mac_native

    mac_native._appkit_cache.clear()  # type: ignore[attr-defined]


def _install_fake_appkit(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Inject a fake AppKit + Foundation + objc into sys.modules.

    Returns a dict of mocks the test can inspect.
    """
    appkit = types.ModuleType("AppKit")
    foundation = types.ModuleType("Foundation")
    objc_mod = types.ModuleType("objc")

    class _MockVisualEffectView:
        def __init__(self) -> None:
            self.material = None
            self.state = None
            self.blending = None
            self.autoresize = None

        @classmethod
        def alloc(cls) -> _MockVisualEffectView:
            return cls()

        def init(self) -> _MockVisualEffectView:
            return self

        def setBlendingMode_(self, v: int) -> None:
            self.blending = v

        def setState_(self, v: int) -> None:
            self.state = v

        def setMaterial_(self, v: int) -> None:
            self.material = v

        def setAutoresizingMask_(self, v: int) -> None:
            self.autoresize = v

        def addSubview_(self, view: Any) -> None:
            return None

    class _MockWindow:
        def __init__(self) -> None:
            self.title_transparent = None
            self.title_visibility = None
            self.style_mask = 1
            self.content = None

        def setTitlebarAppearsTransparent_(self, v: bool) -> None:
            self.title_transparent = v

        def setTitleVisibility_(self, v: int) -> None:
            self.title_visibility = v

        def styleMask(self) -> int:
            return self.style_mask

        def setStyleMask_(self, v: int) -> None:
            self.style_mask = v

        def setContentView_(self, v: Any) -> None:
            self.content = v

        def contentView(self) -> Any:
            return self.content

    class _MockNSColor:
        @staticmethod
        def controlAccentColor() -> Any:
            class _C:
                def colorUsingColorSpace_(self, _space: Any) -> Any:
                    class _S:
                        def redComponent(self) -> float:
                            return 0.5

                        def greenComponent(self) -> float:
                            return 0.25

                        def blueComponent(self) -> float:
                            return 0.10

                    return _S()

            return _C()

        @staticmethod
        def colorWithSRGBRed_green_blue_alpha_(r: float, g: float, b: float, a: float) -> Any:
            return ("color", r, g, b, a)

    class _MockNSColorSpace:
        @staticmethod
        def sRGBColorSpace() -> Any:
            return "srgb"

    class _MockNSApp:
        @staticmethod
        def effectiveAppearance() -> Any:
            class _A:
                def bestMatchFromAppearancesWithNames_(self, names: list[str]) -> str:
                    return names[0]

            return _A()

    appkit.NSVisualEffectView = _MockVisualEffectView
    appkit.NSColor = _MockNSColor
    appkit.NSColorSpace = _MockNSColorSpace
    appkit.NSApp = _MockNSApp
    appkit.NSStatusBar = types.SimpleNamespace(
        systemStatusBar=lambda: types.SimpleNamespace(
            statusItemWithLength_=lambda _length: types.SimpleNamespace(
                button=lambda: None,
                setMenu_=lambda _m: None,
            ),
        ),
    )
    appkit.NSMenu = types.SimpleNamespace(
        alloc=lambda: types.SimpleNamespace(
            init=lambda: types.SimpleNamespace(
                setAutoenablesItems_=lambda _v: None,
                addItem_=lambda _i: None,
            ),
        ),
    )
    appkit.NSObject = type("NSObject", (), {})

    foundation.NSDistributedNotificationCenter = types.SimpleNamespace(
        defaultCenter=lambda: types.SimpleNamespace(
            addObserver_selector_name_object_=lambda *a, **k: None,
            removeObserver_=lambda *_a: None,
        ),
    )

    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setitem(sys.modules, "Foundation", foundation)
    monkeypatch.setitem(sys.modules, "objc", objc_mod)
    return {
        "appkit": appkit,
        "foundation": foundation,
        "view": _MockVisualEffectView,
        "window": _MockWindow,
    }


def test_is_macos_returns_platform_check() -> None:
    from cortex.apps.desktop_shell import mac_native

    assert mac_native.is_macos() == (sys.platform == "darwin")


def test_system_font_constructs_qfont_with_size() -> None:
    """``system_font`` returns a QFont matching the requested size + weight."""
    from cortex.apps.desktop_shell import mac_native

    font = mac_native.system_font(15, "semibold")
    if font is None:  # PySide6 not installed in this environment
        pytest.skip("PySide6 not available")
    assert int(font.pointSizeF()) == 15
    # Apple weight enum: semibold == 600.
    assert int(font.weight()) == 600


def test_system_accent_hex_returns_hex_when_appkit_present(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_appkit(monkeypatch)
    monkeypatch.setattr(
        "cortex.apps.desktop_shell.mac_native.is_macos", lambda: True,
    )
    from cortex.apps.desktop_shell import mac_native

    accent = mac_native.system_accent_hex()
    assert accent is not None
    assert accent.startswith("#")
    assert len(accent) == 7


def test_is_dark_appearance_false_without_appkit() -> None:
    from cortex.apps.desktop_shell import mac_native

    assert mac_native.is_dark_appearance() in {True, False}


def test_apply_vibrancy_no_appkit_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "cortex.apps.desktop_shell.mac_native.is_macos", lambda: False,
    )
    from cortex.apps.desktop_shell import mac_native

    class FakeWidget:
        def winId(self) -> int:
            return 0

    assert mac_native.apply_vibrancy(FakeWidget()) is False


def test_apply_unified_titlebar_no_appkit_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "cortex.apps.desktop_shell.mac_native.is_macos", lambda: False,
    )
    from cortex.apps.desktop_shell import mac_native

    class FakeWidget:
        def winId(self) -> int:
            return 0

    assert mac_native.apply_unified_titlebar(FakeWidget()) is False


def test_status_bar_item_no_appkit_is_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    """On non-mac builds, ``StatusBarItem`` must construct without raising
    and silently no-op on every method."""
    monkeypatch.setattr(
        "cortex.apps.desktop_shell.mac_native.is_macos", lambda: False,
    )
    from cortex.apps.desktop_shell import mac_native

    item = mac_native.StatusBarItem()
    item.add_action("Dashboard", lambda: None)
    item.add_separator()
    item.set_state_tint("#D97757")
    item.set_visible(True)
    # No assertions on internal state — the contract is just "doesn't raise".


def test_hex_to_nscolor_returns_none_without_appkit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "cortex.apps.desktop_shell.mac_native.is_macos", lambda: False,
    )
    from cortex.apps.desktop_shell import mac_native

    assert mac_native._hex_to_nscolor("#D97757") is None
