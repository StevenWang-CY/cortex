"""Cross-surface color contracts for readable UI text.

Identity tints and text tints intentionally differ.  The bright terracotta is
used for fills, charts, and dots; small foreground copy uses the darker
``BRAND_ACCENT_TEXT`` token.  These tests keep generated tokens and custom Qt
styles from silently reintroducing sub-AA combinations.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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
    # The YAML stores CSS ``#RRGGBBAA``; the Python surface is emitted in
    # Qt's ``#AARRGGBB`` order so QSS and QColor decode the intended alpha.
    assert tokens.SEMANTIC_DARK["label_tertiary"] == "#8CEBEBF5"
    assert _contrast(composited, tokens.SEMANTIC_DARK["window_bg"]) >= 4.5


def test_alpha_bearing_tokens_are_emitted_in_qt_channel_order() -> None:
    """Regression for the mangled hairlines: Qt reads nine-digit hex as AARRGGBB.

    Emitting the CSS order made ``separator`` decode as an olive line with
    24% alpha and turned the UNKNOWN/HYPO state dots nearly invisible.
    """

    assert tokens.SEMANTIC_LIGHT["separator"] == "#263C3C43"
    assert tokens.SEMANTIC_LIGHT["label_secondary"] == "#4C3C3C43"
    assert tokens.STATE_COLORS["UNKNOWN"] == "#2E3C3C43"
    for palette in (tokens.SEMANTIC_LIGHT, tokens.SEMANTIC_DARK):
        for name, value in palette.items():
            assert value.startswith("#") and len(value) in (7, 9), (name, value)


def test_qt_decodes_emitted_alpha_tokens_with_intended_opacity() -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtGui import QColor

    separator = QColor(tokens.SEMANTIC_LIGHT["separator"])
    assert separator.isValid()
    assert (separator.red(), separator.green(), separator.blue()) == (0x3C, 0x3C, 0x43)
    assert separator.alpha() == 0x26
    tertiary = QColor(tokens.SEMANTIC_DARK["label_tertiary"])
    assert (tertiary.red(), tertiary.green(), tertiary.blue()) == (0xEB, 0xEB, 0xF5)
    assert tertiary.alpha() == 0x8C


def test_semantic_status_text_tokens_clear_aa_on_their_surfaces() -> None:
    light_surfaces = (
        tokens.SEMANTIC_LIGHT["control_bg"],
        tokens.SEMANTIC_LIGHT["grouped_bg"],
        tokens.SEMANTIC_LIGHT["window_bg"],
    )
    for role in ("success_text", "warning_text", "info_text"):
        for surface in light_surfaces:
            ratio = _contrast(tokens.SEMANTIC_LIGHT[role], surface)
            assert ratio >= 4.5, f"light {role} on {surface}: {ratio:.2f}:1"
    dark_surfaces = (
        tokens.SEMANTIC_DARK["control_bg"],
        tokens.SEMANTIC_DARK["grouped_bg"],
        tokens.SEMANTIC_DARK["window_bg"],
    )
    for role in ("success_text", "warning_text", "info_text"):
        for surface in dark_surfaces:
            ratio = _contrast(tokens.SEMANTIC_DARK[role], surface)
            assert ratio >= 4.5, f"dark {role} on {surface}: {ratio:.2f}:1"
    # Inverse label sits on the filled label_primary control in each scheme.
    assert _contrast(tokens.SEMANTIC_LIGHT["label_inverse"], tokens.SEMANTIC_LIGHT["label_primary"]) >= 4.5
    assert _contrast(tokens.SEMANTIC_DARK["label_inverse"], tokens.SEMANTIC_DARK["label_primary"]) >= 4.5


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


# ---------------------------------------------------------------------------
# Desktop shell: text tints on the surfaces they are actually drawn on
# ---------------------------------------------------------------------------

_LIGHT_SURFACES = (
    tokens.SEMANTIC_LIGHT["control_bg"],
    tokens.SEMANTIC_LIGHT["grouped_bg"],
    tokens.SEMANTIC_LIGHT["window_bg"],
)


def _solid(color: str, surface: str) -> str:
    """Resolve ``color`` to an opaque hex over ``surface``.

    Accepts ``#RRGGBB``, Qt ``#AARRGGBB``, and ``rgba(r, g, b, a)``.
    """
    value = color.strip()
    if value.startswith("rgba("):
        parts = [p.strip() for p in value[5:-1].split(",")]
        r, g, b = (int(float(p)) for p in parts[:3])
        alpha = float(parts[3])
        return _composite(f"#{r:02X}{g:02X}{b:02X}", surface, alpha)
    if value.startswith("#") and len(value) == 9:
        alpha = int(value[1:3], 16) / 255.0
        return _composite("#" + value[3:], surface, alpha)
    return value


def test_desktop_text_tokens_clear_aa_on_every_light_surface() -> None:
    """Secondary copy, status text, and the accent link tint are all used
    as small text on the three light surfaces."""
    for name in (
        "CX_TEXT_SECONDARY",
        "CX_SUCCESS_TEXT",
        "CX_WARNING_TEXT",
        "CX_INFO_TEXT",
        "CX_DANGER_TEXT",
        "BRAND_ACCENT_TEXT",
        "CX_DANGER",
    ):
        foreground = getattr(tokens, name)
        for surface in _LIGHT_SURFACES:
            ratio = _contrast(_solid(foreground, surface), surface)
            assert ratio >= 4.5, f"{name} on {surface}: {ratio:.2f}:1"


def test_status_pill_text_clears_aa_on_its_own_tint() -> None:
    """Every status-pill tone pairs a *text* token with a low-alpha fill;
    the pair must clear AA wherever the pill can sit."""
    from cortex.apps.desktop_shell.components import _PILL_TONES

    for tone, (foreground, fill) in _PILL_TONES.items():
        for surface in _LIGHT_SURFACES:
            background = _solid(fill, surface)
            ratio = _contrast(_solid(foreground, background), background)
            assert ratio >= 4.5, f"pill {tone} on {surface}: {ratio:.2f}:1"


def test_raw_fill_tints_are_never_used_as_text() -> None:
    """systemGreen / systemYellow / the biometric identity tints are fills.
    Small text must use the ``*_TEXT`` tokens or secondary copy."""
    import re

    shell = Path(__file__).resolve().parents[2] / "apps" / "desktop_shell"
    forbidden = re.compile(
        r"color:\s*\{(?:BIO_HR|BIO_BLINK|CX_SUCCESS|CX_WARNING|CX_INFO|"
        r"_SUCCESS|_WARNING|SEMANTIC_LIGHT\[.(?:success|warning|info).\])\}"
    )
    offenders: list[str] = []
    for source in sorted(shell.glob("*.py")):
        if source.name == "tokens.py":
            continue
        for lineno, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
            if forbidden.search(line):
                offenders.append(f"{source.name}:{lineno}: {line.strip()}")
    assert not offenders, "\n".join(offenders)


def test_shared_button_recipes_carry_focus_and_pressed_states() -> None:
    import re

    """Every generated button recipe must show a keyboard focus ring and
    press feedback, and reserve the ring's border so focus never shifts
    layout."""
    for name in (
        "BTN_PRIMARY_QSS",
        "BTN_ACCENT_QSS",
        "BTN_GHOST_QSS",
        "BTN_DESTRUCTIVE_QSS",
        "BTN_LINK_QSS",
        "BTN_SEGMENT_QSS",
        "PILL_BUTTON_QSS",
    ):
        recipe = getattr(tokens, name)
        assert ":focus" in recipe, f"{name} has no focus state"
        assert ":pressed" in recipe, f"{name} has no pressed state"
        # The base block paints a constant-width border that the focus
        # state only recolours (or a reserved transparent border), so a
        # focus ring never shifts layout.
        base = recipe.split("}")[0]
        assert re.search(r"border:\s*[12]px", base), (
            f"{name} has no constant-width border to carry the focus ring"
        )
