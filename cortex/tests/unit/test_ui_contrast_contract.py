"""Cross-surface color contracts for readable UI text.

Identity tints and text tints intentionally differ.  The bright terracotta is
used for fills, charts, and dots; small foreground copy uses the darker
``BRAND_ACCENT_TEXT`` token.  These tests keep generated tokens and custom Qt
styles from silently reintroducing sub-AA combinations.
"""

from __future__ import annotations

from pathlib import Path

from cortex.apps.desktop_shell import tokens


def _rgb(hex_code: str) -> tuple[int, int, int]:
    value = hex_code.removeprefix("#")
    if len(value) != 6:
        raise ValueError(f"expected opaque six-digit hex, got {hex_code!r}")
    return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]


def _luminance(hex_code: str) -> float:
    channels = []
    for raw in _rgb(hex_code):
        channel = raw / 255.0
        channels.append(
            channel / 12.92
            if channel <= 0.04045
            else ((channel + 0.055) / 1.055) ** 2.4
        )
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast(foreground: str, background: str) -> float:
    high, low = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _composite(foreground: str, background: str, alpha: float) -> str:
    fg = _rgb(foreground)
    bg = _rgb(background)
    mixed = tuple(
        round(alpha * a + (1.0 - alpha) * b)
        for a, b in zip(fg, bg, strict=True)
    )
    return "#" + "".join(f"{channel:02X}" for channel in mixed)


def test_foreground_accent_clears_aa_on_all_light_surfaces() -> None:
    surfaces = (
        tokens.SEMANTIC_LIGHT["control_bg"],
        tokens.SEMANTIC_LIGHT["grouped_bg"],
        tokens.SEMANTIC_LIGHT["window_bg"],
    )
    for surface in surfaces:
        assert _contrast(tokens.BRAND_ACCENT_TEXT, surface) >= 4.5


def test_state_text_palette_clears_aa_on_all_light_surfaces() -> None:
    surfaces = (
        tokens.SEMANTIC_LIGHT["control_bg"],
        tokens.SEMANTIC_LIGHT["grouped_bg"],
        tokens.SEMANTIC_LIGHT["window_bg"],
    )
    for state, foreground in tokens.STATE_TEXT_COLORS.items():
        for surface in surfaces:
            ratio = _contrast(foreground, surface)
            assert ratio >= 4.5, f"{state} {foreground} on {surface}: {ratio:.2f}:1"


def test_accent_button_interaction_states_each_clear_aa() -> None:
    combinations = (
        (tokens.SEMANTIC_LIGHT["label_primary"], tokens.BRAND_ACCENT),
        ("#111111", tokens.BRAND_ACCENT_HOVER),
        ("#FFFFFF", tokens.BRAND_ACCENT_PRESSED),
    )
    for foreground, background in combinations:
        assert _contrast(foreground, background) >= 4.5


def test_dark_tertiary_copy_alpha_clears_aa() -> None:
    composited = _composite(
        "#EBEBF5",
        tokens.SEMANTIC_DARK["window_bg"],
        0x8C / 255.0,
    )
    assert tokens.SEMANTIC_DARK["label_tertiary"] == "#EBEBF58C"
    assert _contrast(composited, tokens.SEMANTIC_DARK["window_bg"]) >= 4.5


def test_browser_theme_boundary_defines_dark_border_and_text_fallbacks() -> None:
    reset = (
        Path(__file__).resolve().parents[2]
        / "apps"
        / "browser_extension"
        / "page-reset.css"
    ).read_text(encoding="utf-8")
    assert "--cx-label-tertiary: rgba(235, 235, 245, 0.55);" in reset
    assert "--cx-border-subtle: rgba(255, 255, 255, 0.10);" in reset
    assert "--cx-shadow-float:" in reset
