"""
Design tokens for the Cortex desktop shell.

Port of the browser extension's design-tokens.ts to Python constants.
Single source of truth for the desktop UI's visual identity.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Colors — Warm palette inspired by Claude's interface
# ---------------------------------------------------------------------------

# Backgrounds
CX_BG = "#F5F1EC"
CX_BG_SECONDARY = "#EDE8E2"
CX_SURFACE = "#FFFFFF"
CX_TERTIARY = "#EBE6E0"

# Text
CX_TEXT = "#1A1A1A"
CX_TEXT_SECONDARY = "#5C5854"
CX_TEXT_TERTIARY = "#8E8A85"
CX_TEXT_INVERSE = "#FFFFFF"

# Accent
CX_ACCENT = "#D97757"
CX_ACCENT_HOVER = "#C46547"
CX_ACCENT_DIM = "rgba(217, 119, 87, 0.12)"
CX_ACCENT_SUBTLE = "rgba(217, 119, 87, 0.06)"

# Success
CX_SUCCESS = "#4CAF7D"
CX_SUCCESS_DIM = "rgba(76, 175, 125, 0.10)"

# Danger
CX_DANGER = "#D95757"
CX_DANGER_DIM = "rgba(217, 87, 87, 0.10)"

# Biometrics
CX_BIO_HR = "#D97757"
CX_BIO_HRV = "#57A0D9"
CX_BIO_RESP = "#57D99E"
CX_BIO_BLINK = "#D9B457"

# Borders
CX_BORDER = "rgba(0, 0, 0, 0.06)"
CX_BORDER_DEFAULT = "rgba(0, 0, 0, 0.08)"
CX_BORDER_EMPHASIZED = "rgba(0, 0, 0, 0.15)"

# Shadows (for QGraphicsDropShadowEffect)
CX_SHADOW_FLOAT = "0 8px 32px rgba(0, 0, 0, 0.08), 0 0 0 1px rgba(0, 0, 0, 0.04)"

# ---------------------------------------------------------------------------
# State colors
# ---------------------------------------------------------------------------

STATE_COLORS: dict[str, str] = {
    "FLOW": "#D97757",
    "HYPER": "#BD4932",
    "HYPO": "#8E8A85",
    "RECOVERY": "#57A0D9",
}

STATE_LABELS: dict[str, str] = {
    "FLOW": "Focused",
    "HYPER": "Elevated",
    "HYPO": "Idle",
    "RECOVERY": "Recovering",
}

# ---------------------------------------------------------------------------
# Typography — system-native fonts for crisp rendering
# ---------------------------------------------------------------------------

CX_FONT_SANS = "'Helvetica Neue', 'Segoe UI', sans-serif"
CX_FONT_DISPLAY = "'Helvetica Neue', 'Segoe UI', sans-serif"
CX_FONT_SERIF = "Georgia, 'Times New Roman', ui-serif, serif"
CX_FONT_BRAND = "Georgia, ui-serif, serif"
CX_FONT_MONO = "'Menlo', 'Courier New', monospace"

# ---------------------------------------------------------------------------
# Spacing (4px grid)
# ---------------------------------------------------------------------------

SP1 = 4
SP2 = 8
SP3 = 12
SP4 = 16
SP5 = 20
SP6 = 24
SP7 = 28
SP8 = 32
SP10 = 40

# ---------------------------------------------------------------------------
# Border radii
# ---------------------------------------------------------------------------

RADIUS_XS = 6
RADIUS_SM = 8
RADIUS_MD = 12
RADIUS_LG = 16
RADIUS_XL = 24
RADIUS_FULL = 9999  # Pill shape

# ---------------------------------------------------------------------------
# Animation durations (ms)
# ---------------------------------------------------------------------------

DURATION_MICRO = 100
DURATION_FAST = 150
DURATION_NORMAL = 200
DURATION_SLOW = 400
DURATION_AMBIENT = 3000

# ---------------------------------------------------------------------------
# Dimensions
# ---------------------------------------------------------------------------

DASHBOARD_WIDTH = 400
DASHBOARD_MAX_HEIGHT = 720
HEADER_HEIGHT = 52
GOAL_INPUT_HEIGHT = 42
TOGGLE_TRACK_W = 40
TOGGLE_TRACK_H = 24
TOGGLE_THUMB = 20

# ---------------------------------------------------------------------------
# Shared QSS fragments
# ---------------------------------------------------------------------------

# Common card style
CARD_QSS = f"""
    background: {CX_SURFACE};
    border: 1px solid {CX_BORDER_DEFAULT};
    border-radius: {RADIUS_LG}px;
"""

# Primary button (dark)
BTN_PRIMARY_QSS = f"""
    QPushButton {{
        padding: 10px 20px;
        border-radius: {RADIUS_SM}px;
        background: {CX_TEXT};
        color: {CX_TEXT_INVERSE};
        font-family: {CX_FONT_SANS};
        font-size: 13px;
        font-weight: 600;
        border: none;
    }}
    QPushButton:hover {{ background: #333333; }}
    QPushButton:pressed {{ background: #444444; }}
    QPushButton:disabled {{ background: {CX_TERTIARY}; color: {CX_TEXT_TERTIARY}; }}
"""

# Accent button (terracotta)
BTN_ACCENT_QSS = f"""
    QPushButton {{
        padding: 10px 20px;
        border-radius: {RADIUS_SM}px;
        background: {CX_ACCENT};
        color: {CX_TEXT_INVERSE};
        font-family: {CX_FONT_SANS};
        font-size: 13px;
        font-weight: 600;
        border: none;
    }}
    QPushButton:hover {{ background: {CX_ACCENT_HOVER}; }}
    QPushButton:pressed {{ background: #B35A3D; }}
"""

# Ghost/outline button
BTN_GHOST_QSS = f"""
    QPushButton {{
        padding: 10px 20px;
        border-radius: {RADIUS_SM}px;
        background: transparent;
        color: {CX_TEXT_SECONDARY};
        font-family: {CX_FONT_SANS};
        font-size: 13px;
        font-weight: 500;
        border: 1px solid {CX_BORDER_EMPHASIZED};
    }}
    QPushButton:hover {{ background: rgba(0,0,0,0.03); color: {CX_TEXT}; }}
"""

# Section heading
SECTION_HEADING_QSS = f"""
    font-family: {CX_FONT_SANS};
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 1.2px;
    color: {CX_TEXT_TERTIARY};
    background: transparent;
"""

# Page title
PAGE_TITLE_QSS = f"""
    font-family: {CX_FONT_DISPLAY};
    font-size: 20px;
    font-weight: 700;
    color: {CX_TEXT};
    background: transparent;
"""
