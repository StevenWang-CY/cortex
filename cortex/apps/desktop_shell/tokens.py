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
CX_BG = "#F3EFEA"
CX_SURFACE = "#FFFFFF"
CX_TERTIARY = "#EBE6E0"

# Text
CX_TEXT = "#1A1A1A"
CX_TEXT_SECONDARY = "#66625D"
CX_TEXT_TERTIARY = "#999590"
CX_TEXT_INVERSE = "#FFFFFF"

# Accent
CX_ACCENT = "#D97757"
CX_ACCENT_HOVER = "#C46547"
CX_ACCENT_DIM = "rgba(217, 119, 87, 0.12)"

# Danger
CX_DANGER = "#D95757"
CX_DANGER_DIM = "rgba(217, 87, 87, 0.10)"

# Biometrics
CX_BIO_HR = "#D97757"
CX_BIO_HRV = "#57A0D9"
CX_BIO_RESP = "#57D99E"
CX_BIO_BLINK = "#D9B457"

# Borders
CX_BORDER = "rgba(0, 0, 0, 0.08)"
CX_BORDER_DEFAULT = "rgba(0, 0, 0, 0.12)"
CX_BORDER_EMPHASIZED = "rgba(0, 0, 0, 0.20)"

# Shadows
CX_SHADOW_FLOAT = "0 8px 32px rgba(0, 0, 0, 0.08), 0 0 0 1px rgba(0, 0, 0, 0.04)"

# ---------------------------------------------------------------------------
# State colors
# ---------------------------------------------------------------------------

STATE_COLORS: dict[str, str] = {
    "FLOW": "#D97757",
    "HYPER": "#BD4932",
    "HYPO": "#66625D",
    "RECOVERY": "#57A0D9",
}

STATE_LABELS: dict[str, str] = {
    "FLOW": "Focused",
    "HYPER": "Elevated",
    "HYPO": "Idle",
    "RECOVERY": "Recovering",
}

# ---------------------------------------------------------------------------
# Typography
# ---------------------------------------------------------------------------

CX_FONT_SANS = "'Inter', -apple-system, BlinkMacSystemFont, system-ui, sans-serif"
CX_FONT_SERIF = "ui-serif, 'Georgia', 'Cambria', 'Times New Roman', serif"
CX_FONT_BRAND = "'Cormorant Garamond', ui-serif, 'Georgia', serif"
CX_FONT_MONO = "'JetBrains Mono', 'SF Mono', 'Cascadia Code', monospace"

# ---------------------------------------------------------------------------
# Spacing (4px grid)
# ---------------------------------------------------------------------------

SP1 = 4
SP2 = 8
SP3 = 12
SP4 = 16
SP5 = 20
SP6 = 24
SP8 = 32

# ---------------------------------------------------------------------------
# Border radii
# ---------------------------------------------------------------------------

RADIUS_SM = 8
RADIUS_MD = 16
RADIUS_LG = 24
RADIUS_XL = 32
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

DASHBOARD_WIDTH = 380
DASHBOARD_MAX_HEIGHT = 700
HEADER_HEIGHT = 48
GOAL_INPUT_HEIGHT = 44
TOGGLE_TRACK_W = 40
TOGGLE_TRACK_H = 24
TOGGLE_THUMB = 20
