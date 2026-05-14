"""macOS AppKit bridge — keeps the desktop shell visually native.

This module is the single point where Qt widgets cross over into AppKit so
windows pick up real ``NSVisualEffectView`` translucency, a unified title bar,
the user's effective appearance (light/dark), the system menu bar, and SF
system fonts. Every entry point is guarded so non-mac harnesses + headless
tests stub cleanly — see :func:`is_macos`.

Usage pattern (called once per window after construction)::

    from cortex.apps.desktop_shell import mac_native

    window.show()  # must be shown so winId() is non-zero
    mac_native.apply_unified_titlebar(window)
    mac_native.apply_vibrancy(window, material="window_background")

Brand identity is *preserved* — the system accent color is read for focus
rings only; the Cortex terracotta accent is layered on top.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from typing import Any, Literal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Platform guard — all AppKit calls live behind this. Tests stub via
# ``monkeypatch.setattr("cortex.apps.desktop_shell.mac_native.is_macos",
#                      lambda: True)`` and patch the AppKit symbols.
# ---------------------------------------------------------------------------


def is_macos() -> bool:
    return sys.platform == "darwin"


# Vibrancy materials. Names match the AppKit enum
# ``NSVisualEffectMaterial`` values exposed by pyobjc.
Material = Literal[
    "window_background",  # ``NSVisualEffectMaterialWindowBackground`` (default chrome)
    "sidebar",            # ``NSVisualEffectMaterialSidebar`` (Mail-style sidebar)
    "hudWindow",          # ``NSVisualEffectMaterialHUDWindow`` (Spotlight overlay)
    "popover",            # ``NSVisualEffectMaterialPopover``
    "menu",               # ``NSVisualEffectMaterialMenu``
    "titlebar",           # ``NSVisualEffectMaterialTitlebar``
]


_MATERIAL_INDEX: dict[str, int] = {
    # AppKit raw values for NSVisualEffectMaterial. Stable since 10.14.
    "window_background": 12,
    "sidebar": 7,
    "hudWindow": 13,
    "popover": 6,
    "menu": 5,
    "titlebar": 3,
}


# ---------------------------------------------------------------------------
# Lazy AppKit / Cocoa imports
# ---------------------------------------------------------------------------

_appkit_cache: dict[str, Any] = {}


def _appkit() -> Any | None:
    """Return the AppKit module if available, else None."""
    if "AppKit" in _appkit_cache:
        return _appkit_cache["AppKit"]
    if not is_macos():
        _appkit_cache["AppKit"] = None
        return None
    try:
        import AppKit  # type: ignore[import-not-found]

        _appkit_cache["AppKit"] = AppKit
        return AppKit
    except Exception as exc:  # pragma: no cover - mac-only branch
        logger.debug("AppKit unavailable: %s", exc)
        _appkit_cache["AppKit"] = None
        return None


def _objc() -> Any | None:
    if "objc" in _appkit_cache:
        return _appkit_cache["objc"]
    if not is_macos():
        _appkit_cache["objc"] = None
        return None
    try:
        import objc  # type: ignore[import-not-found]

        _appkit_cache["objc"] = objc
        return objc
    except Exception:  # pragma: no cover
        _appkit_cache["objc"] = None
        return None


def _ns_window_for(widget: Any) -> Any | None:
    """Resolve the ``NSWindow`` backing a Qt widget via its ``winId``.

    Returns ``None`` on non-mac or when the widget is not yet realized.
    """
    AppKit = _appkit()
    if AppKit is None:
        return None
    try:
        wid = int(widget.winId())
    except Exception:
        return None
    if wid == 0:
        return None
    try:
        # NSView pointer comes back from winId(); ask its window.
        view = _objc().objc_object(c_void_p=wid)  # type: ignore[union-attr]
        return view.window() if view is not None else None
    except Exception as exc:  # pragma: no cover - mac-only path
        logger.debug("Cannot resolve NSWindow for widget: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def apply_vibrancy(widget: Any, material: Material = "window_background") -> bool:
    """Install an ``NSVisualEffectView`` behind the widget's NSWindow content.

    Returns True on success, False if AppKit unavailable or widget not realized.
    The widget should already be ``show()``-n so its ``winId()`` is non-zero.

    The terracotta brand surfaces (state badge, intervention card, etc.) stay
    on top of the vibrant material because Qt views are added as subviews of
    the effect view (the AppKit "back to front" stacking rule).
    """
    AppKit = _appkit()
    if AppKit is None:
        return False
    window = _ns_window_for(widget)
    if window is None:
        return False
    try:
        effect_view = AppKit.NSVisualEffectView.alloc().init()
        effect_view.setBlendingMode_(0)  # behindWindow
        effect_view.setState_(1)  # active
        effect_view.setMaterial_(_MATERIAL_INDEX.get(material, 12))
        effect_view.setAutoresizingMask_(18)  # width+height resizable
        window.setContentView_(effect_view)
        # Re-parent the original Qt content view as a subview.
        content = window.contentView()
        if content is not effect_view:
            effect_view.addSubview_(content)
        return True
    except Exception as exc:  # pragma: no cover - mac-only
        logger.debug("apply_vibrancy failed: %s", exc)
        return False


def apply_unified_titlebar(widget: Any, *, transparent: bool = True) -> bool:
    """Hide the title text, draw the title bar transparently, expand content
    edge-to-edge under the traffic lights (the "full size content view"
    pattern used by Safari, Mail, Music, System Settings)."""
    AppKit = _appkit()
    if AppKit is None:
        return False
    window = _ns_window_for(widget)
    if window is None:
        return False
    try:
        window.setTitlebarAppearsTransparent_(bool(transparent))
        window.setTitleVisibility_(1)  # NSWindowTitleHidden
        mask = window.styleMask()
        mask |= 1 << 15  # NSWindowStyleMaskFullSizeContentView
        window.setStyleMask_(mask)
        return True
    except Exception as exc:  # pragma: no cover
        logger.debug("apply_unified_titlebar failed: %s", exc)
        return False


def is_dark_appearance() -> bool:
    """Return True when the user's effective appearance is dark.

    Updated each call (cheap) — used by the token resolver in ``tokens.py``
    so palette flips without a restart when System Settings changes.
    """
    AppKit = _appkit()
    if AppKit is None:
        return False
    try:
        app = AppKit.NSApp
        if app is None:
            return False
        appearance = app.effectiveAppearance()
        if appearance is None:
            return False
        name = appearance.bestMatchFromAppearancesWithNames_(
            ["NSAppearanceNameDarkAqua", "NSAppearanceNameAqua"]
        )
        return name == "NSAppearanceNameDarkAqua"
    except Exception:  # pragma: no cover
        return False


def system_accent_hex() -> str | None:
    """Hex string of the user's current macOS accent color, or None.

    Cortex *does not* paint with this — the brand terracotta is always used
    for emphasis. We expose it only so focus rings (which Apple's HIG says
    should respect the system accent) can match. See dashboard.py focus
    style on the goal input.
    """
    AppKit = _appkit()
    if AppKit is None:
        return None
    try:
        color = AppKit.NSColor.controlAccentColor()
        srgb = color.colorUsingColorSpace_(AppKit.NSColorSpace.sRGBColorSpace())
        r = int(round(srgb.redComponent() * 255))
        g = int(round(srgb.greenComponent() * 255))
        b = int(round(srgb.blueComponent() * 255))
        return f"#{r:02X}{g:02X}{b:02X}"
    except Exception:  # pragma: no cover
        return None


# Maps the names used in ``tokens.yaml`` to the AppKit weight constants.
_WEIGHT_MAP: dict[str, float] = {
    "ultralight": -0.8,
    "thin": -0.6,
    "light": -0.4,
    "regular": 0.0,
    "medium": 0.23,
    "semibold": 0.3,
    "bold": 0.4,
    "heavy": 0.56,
    "black": 0.62,
}


def system_font(point_size: float, weight: str = "regular") -> Any:
    """Construct a ``QFont`` whose family matches the macOS system font.

    On macOS this uses the special family name ``".AppleSystemUIFont"`` so
    Qt picks up SF Pro Text / SF Pro Display automatically based on size.
    On other platforms we fall back to ``-apple-system`` font-family chains.
    """
    try:
        from PySide6.QtGui import QFont

        font = QFont()
        if is_macos():
            font.setFamily(".AppleSystemUIFont")
        else:
            # Linux/Windows fallback chain (frontend-design system stack).
            font.setFamilies([
                "SF Pro Text",
                "SF Pro Display",
                "Segoe UI",
                "system-ui",
                "Helvetica Neue",
            ])
        font.setPointSizeF(float(point_size))
        weight_key = weight.lower()
        # PySide6 QFont.Weight enums are 1..99 — we map our token names.
        qt_weight_map = {
            "ultralight": 100,
            "thin": 200,
            "light": 300,
            "regular": 400,
            "medium": 500,
            "semibold": 600,
            "bold": 700,
            "heavy": 800,
            "black": 900,
        }
        font.setWeight(QFont.Weight(qt_weight_map.get(weight_key, 400)))
        return font
    except Exception as exc:  # pragma: no cover - PySide6 unavailable
        logger.debug("system_font fallback: %s", exc)
        return None


def install_appearance_observer(callback: Callable[[bool], None]) -> Callable[[], None] | None:
    """Call ``callback(is_dark)`` whenever the user toggles light/dark mode.

    Returns a teardown function (or None if AppKit unavailable). The callback
    runs on the AppKit notification queue — call sites that need to update Qt
    widgets must marshal through ``QTimer.singleShot(0, ...)``.
    """
    AppKit = _appkit()
    objc = _objc()
    if AppKit is None or objc is None:
        return None
    try:
        from Foundation import NSDistributedNotificationCenter  # type: ignore[import-not-found]

        center = NSDistributedNotificationCenter.defaultCenter()

        class _Observer(AppKit.NSObject):  # type: ignore[misc]
            def appearanceChanged_(self, _note: Any) -> None:
                try:
                    callback(is_dark_appearance())
                except Exception:
                    logger.debug("appearance callback raised", exc_info=True)

        observer = _Observer.alloc().init()
        center.addObserver_selector_name_object_(
            observer,
            objc.selector(observer.appearanceChanged_, signature=b"v@:@"),
            "AppleInterfaceThemeChangedNotification",
            None,
        )

        def teardown() -> None:
            try:
                center.removeObserver_(observer)
            except Exception:
                pass

        return teardown
    except Exception as exc:  # pragma: no cover
        logger.debug("install_appearance_observer failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Status bar (NSStatusItem) — replaces QSystemTrayIcon on mac so the menu bar
# icon picks up real templated appearance / dark menu bar / accent tinting.
# ---------------------------------------------------------------------------


class StatusBarItem:
    """Thin wrapper around a real ``NSStatusItem`` + ``NSMenu``.

    Falls back to a no-op shell on non-mac so callers don't need a branch.
    """

    def __init__(self, *, title: str = "Cortex", template_symbol: str = "heart.fill") -> None:
        self._title = title
        self._template_symbol = template_symbol
        self._appkit = _appkit()
        self._item: Any = None
        self._menu: Any = None
        self._actions: list[Any] = []  # keep references alive
        if self._appkit is not None:
            self._build_native()

    # -- Native implementation --------------------------------------------

    def _build_native(self) -> None:
        AppKit = self._appkit
        if AppKit is None:
            return
        try:
            bar = AppKit.NSStatusBar.systemStatusBar()
            length = -1.0  # NSVariableStatusItemLength
            self._item = bar.statusItemWithLength_(length)
            self._refresh_button_icon()
            self._menu = AppKit.NSMenu.alloc().init()
            self._menu.setAutoenablesItems_(False)
            self._item.setMenu_(self._menu)
        except Exception as exc:  # pragma: no cover
            logger.debug("StatusBarItem build failed: %s", exc)
            self._item = None
            self._menu = None

    def _refresh_button_icon(self) -> None:
        AppKit = self._appkit
        if AppKit is None or self._item is None:
            return
        try:
            button = self._item.button()
            if button is None:
                return
            image = None
            if hasattr(AppKit.NSImage, "imageWithSystemSymbolName_accessibilityDescription_"):
                image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                    self._template_symbol, "Cortex",
                )
            if image is not None:
                image.setTemplate_(True)
                button.setImage_(image)
                button.setTitle_("")
            else:
                button.setTitle_(self._title)
        except Exception:  # pragma: no cover
            logger.debug("status item image refresh failed", exc_info=True)

    def set_state_tint(self, hex_color: str | None) -> None:
        """Tint the templated icon to reflect Cortex state (terracotta on
        HYPER, etc.). Pass ``None`` to clear."""
        AppKit = self._appkit
        if AppKit is None or self._item is None:
            return
        try:
            button = self._item.button()
            if button is None:
                return
            if hex_color is None:
                button.setContentTintColor_(None)
                return
            color = _hex_to_nscolor(hex_color)
            if color is not None:
                button.setContentTintColor_(color)
        except Exception:  # pragma: no cover
            logger.debug("status item tint failed", exc_info=True)

    def add_action(self, title: str, callback: Callable[[], None] | None,
                   *, key: str = "", enabled: bool = True) -> None:
        AppKit = self._appkit
        if AppKit is None or self._menu is None:
            return
        try:
            target = _MenuActionTarget.alloc().init()
            target.callback = callback  # type: ignore[attr-defined]
            item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                title,
                _objc().selector(target.fire_, signature=b"v@:@"),  # type: ignore[union-attr]
                key,
            )
            item.setTarget_(target)
            item.setEnabled_(bool(enabled))
            self._menu.addItem_(item)
            self._actions.append(target)  # keep alive
        except Exception:  # pragma: no cover
            logger.debug("add_action failed", exc_info=True)

    def add_separator(self) -> None:
        if self._appkit is None or self._menu is None:
            return
        try:
            sep = self._appkit.NSMenuItem.separatorItem()
            self._menu.addItem_(sep)
        except Exception:  # pragma: no cover
            pass

    def set_visible(self, visible: bool) -> None:
        if self._item is not None and hasattr(self._item, "setVisible_"):
            self._item.setVisible_(bool(visible))


# Objective-C selector target shim. Defined at module scope so the runtime
# can register it once.
def _make_action_target_class() -> Any | None:
    AppKit = _appkit()
    if AppKit is None:
        return None
    try:
        class _Target(AppKit.NSObject):  # type: ignore[misc]
            def fire_(self, _sender: Any) -> None:
                cb = getattr(self, "callback", None)
                if cb is None:
                    return
                try:
                    cb()
                except Exception:
                    logger.debug("status menu callback raised", exc_info=True)

        return _Target
    except Exception:
        return None


_MenuActionTarget = _make_action_target_class()


def _hex_to_nscolor(hex_color: str) -> Any | None:
    AppKit = _appkit()
    if AppKit is None:
        return None
    s = hex_color.lstrip("#")
    if len(s) == 6:
        try:
            r = int(s[0:2], 16) / 255.0
            g = int(s[2:4], 16) / 255.0
            b = int(s[4:6], 16) / 255.0
            return AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, 1.0)
        except Exception:  # pragma: no cover
            return None
    return None


__all__ = [
    "Material",
    "StatusBarItem",
    "apply_unified_titlebar",
    "apply_vibrancy",
    "install_appearance_observer",
    "is_dark_appearance",
    "is_macos",
    "system_accent_hex",
    "system_font",
]
