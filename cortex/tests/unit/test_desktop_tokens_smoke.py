"""F20 (Phase-4 audit) — smoke test for desktop_shell.tokens.

Every exported string token must be a non-empty string. The point of
the test is not the type check itself (mypy already enforces ``str``
typing); it's that a future YAML edit cannot silently collapse a token
to ``""``, which would make Qt widgets render with empty
``background: ;`` rules that silently fall back to the platform
default and break dark-mode contrast.

Tuple/int tokens are also smoke-checked for non-empty / non-zero
values where that's the intended contract.
"""

from __future__ import annotations

import pytest

from cortex.apps.desktop_shell import tokens

STRING_TOKEN_NAMES = [
    "BRAND_ACCENT",
    "BRAND_ACCENT_HOVER",
    "BRAND_ACCENT_PRESSED",
    "BRAND_ACCENT_DARK",
    "BRAND_ACCENT_DIM",
    "BRAND_ACCENT_SUBTLE",
    "BRAND_DISPLAY_FONT",
    "BRAND_WORDMARK",
    "BIO_HR",
    "BIO_HRV",
    "BIO_RESP",
    "BIO_BLINK",
    "FONT_SYSTEM",
    "FONT_DISPLAY",
    "FONT_MONO",
    "CX_BG",
    "CX_BG_SECONDARY",
    "CX_SURFACE",
    "CX_TERTIARY",
    "CX_TEXT",
    "CX_TEXT_SECONDARY",
    "CX_TEXT_TERTIARY",
    "CX_TEXT_INVERSE",
    "CX_ACCENT",
    "CX_ACCENT_HOVER",
    "CX_ACCENT_DIM",
    "CX_ACCENT_SUBTLE",
    "CX_SUCCESS",
    "CX_SUCCESS_DIM",
    "CX_DANGER",
    "CX_DANGER_DIM",
    "CX_BIO_HR",
    "CX_BIO_HRV",
    "CX_BIO_RESP",
    "CX_BIO_BLINK",
    "CX_BORDER",
    "CX_BORDER_DEFAULT",
    "CX_BORDER_EMPHASIZED",
    "CX_SHADOW_FLOAT",
    "CX_FONT_SANS",
    "CX_FONT_DISPLAY",
    "CX_FONT_SERIF",
    "CX_FONT_BRAND",
    "CX_FONT_MONO",
    "CARD_QSS",
    "BTN_PRIMARY_QSS",
    "BTN_ACCENT_QSS",
    "BTN_GHOST_QSS",
    "SECTION_HEADING_QSS",
    "PAGE_TITLE_QSS",
]

INT_TOKEN_NAMES = [
    "FS_CAPTION",
    "FS_FOOTNOTE",
    "FS_BODY",
    "FS_TITLE",
    "FS_LARGE_TITLE",
    "FS_HERO_NUMERIC",
    "FW_REGULAR",
    "FW_MEDIUM",
    "FW_SEMIBOLD",
    "FW_BOLD",
    "SP1",
    "SP2",
    "SP3",
    "SP4",
    "SP5",
    "SP6",
    "SP7",
    "SP8",
    "SP10",
    "RADIUS_WINDOW",
    "RADIUS_CARD",
    "RADIUS_BUTTON",
    "RADIUS_CONTROL",
    "RADIUS_PILL",
    "RADIUS_XS",
    "RADIUS_SM",
    "RADIUS_MD",
    "RADIUS_LG",
    "RADIUS_XL",
    "RADIUS_FULL",
    "DURATION_MICRO",
    "DURATION_FAST",
    "DURATION_NORMAL",
    "DURATION_SLOW",
    "DURATION_AMBIENT",
    "DASHBOARD_WIDTH",
    "DASHBOARD_MAX_HEIGHT",
    "POPUP_WIDTH",
    "POPUP_MAX_HEIGHT",
    "BREATHING_PACER_SIZE",
    "HEADER_HEIGHT",
    "GOAL_INPUT_HEIGHT",
    "TOGGLE_TRACK_W",
    "TOGGLE_TRACK_H",
    "TOGGLE_THUMB",
]


@pytest.mark.parametrize("name", STRING_TOKEN_NAMES)
def test_string_tokens_are_non_empty(name: str) -> None:
    value = getattr(tokens, name)
    assert isinstance(value, str), f"{name} is not a string: {type(value)!r}"
    assert value != "", f"{name} is empty"


@pytest.mark.parametrize("name", INT_TOKEN_NAMES)
def test_int_tokens_are_positive(name: str) -> None:
    value = getattr(tokens, name)
    assert isinstance(value, int), f"{name} is not an int: {type(value)!r}"
    assert value > 0, f"{name} is non-positive: {value}"


def test_state_color_map_has_canonical_states() -> None:
    """The state color map must cover every state the daemon emits."""
    canonical = {"FLOW", "HYPER", "HYPO", "RECOVERY"}
    assert canonical.issubset(tokens.STATE_COLORS.keys())
    for state in canonical:
        color = tokens.STATE_COLORS[state]
        assert isinstance(color, str) and color != ""


def test_state_label_map_covers_canonical_states() -> None:
    canonical = {"FLOW", "HYPER", "HYPO", "RECOVERY"}
    assert canonical.issubset(tokens.STATE_LABELS.keys())
    for state in canonical:
        label = tokens.STATE_LABELS[state]
        assert isinstance(label, str) and label != ""


def test_palette_variants_cover_canonical_states() -> None:
    canonical = {"FLOW", "HYPER", "HYPO", "RECOVERY"}
    for variant_name, palette in tokens.PALETTE_VARIANTS.items():
        missing = canonical - set(palette.keys())
        assert not missing, (
            f"PALETTE_VARIANTS[{variant_name!r}] is missing {missing}"
        )
        for state in canonical:
            assert palette[state] != "", (
                f"PALETTE_VARIANTS[{variant_name!r}][{state!r}] is empty"
            )
