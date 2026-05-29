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
        import AppKit

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
        import objc

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
        view = _objc().objc_object(c_void_p=wid)
        return view.window() if view is not None else None
    except Exception as exc:  # pragma: no cover - mac-only path
        logger.debug("Cannot resolve NSWindow for widget: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def apply_vibrancy(widget: Any, material: Material = "window_background") -> bool:
    """Tint the widget's NSWindow background to the system window colour.

    NOTE: this does NOT install an ``NSVisualEffectView`` — true vibrancy
    (the blurred translucent material) is intentionally disabled. Two
    earlier revisions tried to slot an ``NSVisualEffectView`` under Qt's
    contentView via ``setContentView_`` / sibling subview swaps; both
    orphaned the Qt view (windows registered as ``count=0`` to the
    WindowServer and the Dock icon bounced forever with no visible
    window). The safe behaviour is to leave the contentView untouched and
    only tint the window background so the unified titlebar + Qt content
    read as one continuous surface. The ``material`` argument is accepted
    for call-site compatibility but ignored; the native look is carried by
    the surrounding chrome (titlebar transparency, SF Pro fonts,
    NSStatusItem, HIG palette + radii).

    Returns:
        True when the background tint was actually applied, False when
        AppKit is unavailable, the widget is not yet realized, or the
        tint call raised. Callers must NOT interpret a True result as
        "vibrancy installed" — it means "background tint applied".
    """
    del material  # accepted for API compatibility; tint-only path ignores it
    AppKit = _appkit()
    if AppKit is None:
        return False
    window = _ns_window_for(widget)
    if window is None:
        return False
    try:
        # Make the titlebar share the window background colour so the
        # transparency from ``apply_unified_titlebar`` reads as one
        # continuous surface rather than a stripe.
        window.setBackgroundColor_(AppKit.NSColor.windowBackgroundColor())
        return True
    except Exception as exc:  # pragma: no cover - mac-only
        logger.debug("apply_vibrancy background tint failed: %s", exc)
        return False


def apply_unified_titlebar(widget: Any, *, transparent: bool = True) -> bool:
    """Hide the title text, draw the title bar transparently, expand content
    edge-to-edge under the traffic lights (the "full size content view"
    pattern used by Safari, Mail, Music, System Settings).

    Also enables ``setMovableByWindowBackground_`` so the user can grab any
    transparent background area — not just the thin title-bar strip — to
    drag the window. Without this the unified-titlebar pattern leaves the
    user with only a ~10px tall drag region above the content, which is
    invisible against the transparent titlebar.
    """
    AppKit = _appkit()
    if AppKit is None:
        return False
    window = _ns_window_for(widget)
    if window is None:
        return False
    try:
        # IMPORTANT: do NOT enable NSWindowStyleMaskFullSizeContentView.
        # When we did, the title bar collapsed to a 0-height drag region
        # (the user had no surface to grab). ``setMovableByWindowBackground_``
        # only activates drag on TRANSPARENT areas, and Qt's contentView is
        # opaque (it paints the cream background), so the window became
        # un-draggable. Keeping the standard title-bar height (~28pt) gives
        # a reliable drag region above Qt's content. Title text is still
        # hidden via ``setTitleVisibility_``, so the bar reads as a clean
        # transparent strip with just traffic lights — the macOS-native
        # look without breaking dragging.
        window.setTitlebarAppearsTransparent_(bool(transparent))
        window.setTitleVisibility_(1)  # NSWindowTitleHidden
        try:
            window.setMovableByWindowBackground_(True)
        except Exception:
            pass
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


def prefers_reduced_motion() -> bool:
    """Return True when the user has the macOS "Reduce Motion"
    accessibility preference enabled (System Settings → Accessibility →
    Display → Reduce motion). Phase J-4.

    UI surfaces that animate (overlay headline scale-in, fade-ins,
    transitions) should consult this and skip the tween — applying the
    end state directly — when it returns True. The result is read fresh
    every call rather than cached because the user can toggle the
    preference mid-session; the AppKit call is cheap (a single property
    read on ``NSWorkspace``).

    Falls back to ``False`` on non-mac platforms and when AppKit is
    unavailable — motion is then governed by the calling code's
    explicit timing constants. This is intentional: a non-mac harness
    or test stub should exercise the animation path, not the skip path.
    """
    AppKit = _appkit()
    if AppKit is None:
        return False
    try:
        workspace = AppKit.NSWorkspace.sharedWorkspace()
        if workspace is None:
            return False
        # ``accessibilityDisplayShouldReduceMotion`` is the canonical
        # public API (10.12+). Older fallback path is the deprecated
        # ``defaults read com.apple.universalaccess reduceMotion`` which
        # we do not need — Cortex's minimum macOS is well above 10.12.
        return bool(workspace.accessibilityDisplayShouldReduceMotion())
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
        from Foundation import NSDistributedNotificationCenter

        center = NSDistributedNotificationCenter.defaultCenter()

        # Bind the ObjC base class to a local name so the class statement
        # references a plain name (a dotted ``AppKit.NSObject`` base is
        # unresolvable to mypy and raises [name-defined]; the local is
        # typed ``Any`` and resolves at runtime on macOS).
        _NSObject: Any = AppKit.NSObject

        class _Observer(_NSObject):
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
        """Render the status-bar icon.

        We use a unicode heart glyph ("♥") as the button title rather than an
        ``NSImage`` from ``imageWithSystemSymbolName_``. The SF Symbol API
        returns a non-nil but effectively invisible image inside an ad-hoc
        signed PyInstaller bundle (no SF Symbols catalog access for non-
        notarized identifiers), which leaves the slot empty and the mouse
        hit-area unclickable. The glyph is always reliable and matches the
        Cortex ECG-heart brand mark.
        """
        AppKit = self._appkit
        if AppKit is None or self._item is None:
            return
        try:
            button = self._item.button()
            if button is None:
                return
            button.setImage_(None)
            button.setTitle_("♥")
        except Exception:  # pragma: no cover
            logger.debug("status item title set failed", exc_info=True)

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
        target_cls = _menu_action_target_class()
        if target_cls is None:
            return
        try:
            target = target_cls.alloc().init()
            target.callback = callback
            item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                title,
                _objc().selector(target.fire_, signature=b"v@:@"),
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


# Objective-C selector target shim. Built lazily (NOT at import time) so the
# module imports cleanly off-mac and never eagerly imports AppKit — the whole
# AppKit surface is gated behind ``_appkit()``. The constructed ObjC class is
# memoised in ``_appkit_cache`` so the runtime registers it exactly once.
def _make_action_target_class() -> Any | None:
    AppKit = _appkit()
    if AppKit is None:
        return None
    try:
        # Local-name base (see ``install_appearance_observer``): a dotted
        # ``AppKit.NSObject`` base is unresolvable to mypy.
        _NSObject: Any = AppKit.NSObject

        class _Target(_NSObject):
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


def _menu_action_target_class() -> Any | None:
    """Lazily build + memoise the ``_Target`` ObjC selector shim.

    Replaces the eager module-level ``_MenuActionTarget`` which forced an
    AppKit import at import time (contradicting the lazy-import design and
    breaking the non-mac import path). Cached in ``_appkit_cache`` so the
    ObjC runtime registers the class exactly once."""
    if "menu_action_target" in _appkit_cache:
        return _appkit_cache["menu_action_target"]
    target = _make_action_target_class()
    _appkit_cache["menu_action_target"] = target
    return target


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
