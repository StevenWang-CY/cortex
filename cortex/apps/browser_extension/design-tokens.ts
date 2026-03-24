/**
 * Cortex Design System — Shared Tokens
 *
 * Single source of truth for all colors, typography, spacing, radius,
 * easing, and state mappings across popup, newtab, and content scripts.
 */

// --- Core Tokens ---

export const CX = {
    // Canvas (Claude-style warm, earthy off-white)
    bg: "#F3EFEA",
    surface: "#FFFFFF",
    tertiary: "#EBE6E0",
    overlay: "rgba(243, 239, 234, 0.88)",

    // Borders
    border: "rgba(0, 0, 0, 0.08)",       
    borderDefault: "rgba(0, 0, 0, 0.12)", 
    borderEmphasis: "rgba(0, 0, 0, 0.20)", 

    // Shadows
    shadowFloat: "0 8px 32px rgba(0, 0, 0, 0.08), 0 0 0 1px rgba(0,0,0,0.04)",

    // Text (High contrast darks)
    text: "#1A1A1A",
    textSecondary: "#66625D",
    textTertiary: "#999590",
    textInverse: "#FFFFFF",

    // Accent (Terracotta/Bronze)
    accent: "#D97757",
    accentHover: "#C46547",
    accentDim: "rgba(217, 119, 87, 0.12)",

    // Danger
    danger: "#D95757",
    dangerDim: "rgba(217, 87, 87, 0.10)",

    // Biometric
    bioHr: "#D97757",     // Terracotta
    bioHrv: "#57A0D9",    // Calm blue
    bioResp: "#57D99E",   // Soft green
    bioBlink: "#D9B457",  // Warm yellow

    // Fonts
    font: "'Inter', -apple-system, BlinkMacSystemFont, system-ui, sans-serif",
    fontSerif: "ui-serif, 'Georgia', 'Cambria', 'Times New Roman', serif",
    fontBrand: "'Cormorant Garamond', ui-serif, 'Georgia', serif",
    mono: "'JetBrains Mono', 'SF Mono', 'Cascadia Code', monospace",

    // Radius (Much softer, more organic)
    radiusSm: 8,
    radiusMd: 16,
    radiusLg: 24,
    radiusXl: 32,
    radiusFull: 9999,

    // Spacing (4px grid)
    space1: 4,
    space2: 8,
    space3: 12,
    space4: 16,
    space5: 20,
    space6: 24,
    space8: 32,

    // Easing
    easeDefault: "cubic-bezier(0.4, 0, 0.2, 1)",
    easeEnter: "cubic-bezier(0, 0, 0.2, 1)",
    easeExit: "cubic-bezier(0.4, 0, 1, 1)",

    // Durations
    durationMicro: "100ms",   
    durationFast: "150ms",    
    durationNormal: "200ms",  
    durationSlow: "400ms",    
    durationAmbient: "3000ms", 
} as const;

// --- State Colors (hex, for React inline styles) ---

export const STATE_COLORS: Record<string, string> = {
    FLOW: "#D97757",    // Focus state -> Terracotta 
    HYPER: "#BD4932",   // Hyper state -> Darker rust
    HYPO: "#66625D",    // Low state -> Warm grey
    RECOVERY: "#57A0D9", // Recovering -> Soft blue
};

// --- State Colors (RGB, for canvas rendering) ---

export const STATE_COLORS_RGB: Record<string, { r: number; g: number; b: number }> = {
    FLOW: { r: 217, g: 119, b: 87 },
    HYPER: { r: 189, g: 73, b: 50 },
    HYPO: { r: 102, g: 98, b: 93 },
    RECOVERY: { r: 87, g: 160, b: 217 },
};

// --- State Colors (muted, 12% opacity backgrounds) ---

export const STATE_COLORS_MUTED: Record<string, string> = {
    FLOW: "rgba(217, 119, 87, 0.12)",
    HYPER: "rgba(189, 73, 50, 0.12)",
    HYPO: "rgba(102, 98, 93, 0.12)",
    RECOVERY: "rgba(87, 160, 217, 0.12)",
};

// --- State Labels ---

export const STATE_LABELS: Record<string, string> = {
    FLOW: "Focused",
    HYPER: "Elevated",
    HYPO: "Idle",
    RECOVERY: "Recovering",
};

// --- Somatic Temperatures (ambient color filter) ---

export const SOMATIC_TEMPS: Record<string, { r: number; g: number; b: number; opacity: number }> = {
    FLOW: { r: 217, g: 119, b: 87, opacity: 0.015 },     
    HYPER: { r: 189, g: 73, b: 50, opacity: 0.03 },      
    HYPO: { r: 102, g: 98, b: 93, opacity: 0 },         
    RECOVERY: { r: 87, g: 160, b: 217, opacity: 0.02 },
};

// --- Font Loading ---

/**
 * Bundled @font-face declarations. Fonts are shipped in assets/fonts/
 * and declared as web_accessible_resources in the manifest.
 * chrome.runtime.getURL resolves the extension-internal path at runtime.
 * Falls back to system fonts if the extension context is unavailable.
 */
function fontURL(filename: string): string {
    try {
        return chrome.runtime.getURL(`assets/fonts/${filename}`);
    } catch {
        return "";
    }
}

export function fontFaceCSS(): string {
    const interLatin = fontURL("inter-latin.woff2");
    const interLatinExt = fontURL("inter-latin-ext.woff2");
    const jbLatin = fontURL("jetbrains-mono-latin.woff2");
    const jbLatinExt = fontURL("jetbrains-mono-latin-ext.woff2");

    // If extension context is unavailable, return empty (system fonts will handle it)
    if (!interLatin) return "";

    return `
        @font-face {
            font-family: 'Inter';
            font-style: normal;
            font-weight: 400 600;
            font-display: swap;
            src: url('${interLatinExt}') format('woff2');
            unicode-range: U+0100-02BA, U+02BD-02C5, U+02C7-02CC, U+02CE-02D7, U+02DD-02FF, U+0304, U+0308, U+0329, U+1D00-1DBF, U+1E00-1E9F, U+1EF2-1EFF, U+2020, U+20A0-20AB, U+20AD-20C0, U+2113, U+2C60-2C7F, U+A720-A7FF;
        }
        @font-face {
            font-family: 'Inter';
            font-style: normal;
            font-weight: 400 600;
            font-display: swap;
            src: url('${interLatin}') format('woff2');
            unicode-range: U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD;
        }
        @font-face {
            font-family: 'JetBrains Mono';
            font-style: normal;
            font-weight: 400 500;
            font-display: swap;
            src: url('${jbLatinExt}') format('woff2');
            unicode-range: U+0100-02BA, U+02BD-02C5, U+02C7-02CC, U+02CE-02D7, U+02DD-02FF, U+0304, U+0308, U+0329, U+1D00-1DBF, U+1E00-1E9F, U+1EF2-1EFF, U+2020, U+20A0-20AB, U+20AD-20C0, U+2113, U+2C60-2C7F, U+A720-A7FF;
        }
        @font-face {
            font-family: 'JetBrains Mono';
            font-style: normal;
            font-weight: 400 500;
            font-display: swap;
            src: url('${jbLatin}') format('woff2');
            unicode-range: U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD;
        }
    `;
}

// Legacy CDN import kept as fallback for environments where chrome.runtime is unavailable
export const FONT_IMPORT_CSS = fontFaceCSS() || `@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap');`;

// --- CSS Custom Properties for Shadow DOM ---

/**
 * Generates a CSS string of custom properties from the CX token object.
 * Inject into each shadow DOM root as `:host { ${cxVars()} }` so that
 * CSS inside templates can use `var(--cx-bg)`, `var(--cx-accent)`, etc.
 * This centralizes token values — change once in CX, all shadow DOMs update.
 */
export function cxVars(): string {
    const map: Record<string, string | number> = {
        "bg": CX.bg,
        "surface": CX.surface,
        "tertiary": CX.tertiary,
        "overlay": CX.overlay,
        "border": CX.border,
        "border-default": CX.borderDefault,
        "border-emphasis": CX.borderEmphasis,
        "text": CX.text,
        "text-secondary": CX.textSecondary,
        "text-tertiary": CX.textTertiary,
        "text-inverse": CX.textInverse,
        "accent": CX.accent,
        "accent-hover": CX.accentHover,
        "accent-dim": CX.accentDim,
        "danger": CX.danger,
        "danger-dim": CX.dangerDim,
        "bio-hr": CX.bioHr,
        "bio-hrv": CX.bioHrv,
        "bio-resp": CX.bioResp,
        "bio-blink": CX.bioBlink,
        "font": CX.font,
        "font-serif": CX.fontSerif,
        "mono": CX.mono,
        "radius-sm": `${CX.radiusSm}px`,
        "radius-md": `${CX.radiusMd}px`,
        "radius-lg": `${CX.radiusLg}px`,
        "radius-xl": `${CX.radiusXl}px`,
        "ease-default": CX.easeDefault,
        "ease-enter": CX.easeEnter,
        "ease-exit": CX.easeExit,
        "duration-micro": CX.durationMicro,
        "duration-fast": CX.durationFast,
        "duration-normal": CX.durationNormal,
        "duration-slow": CX.durationSlow,
        "shadow-float": CX.shadowFloat,
    };
    return Object.entries(map)
        .map(([k, v]) => `--cx-${k}: ${v};`)
        .join("\n    ");
}

/**
 * Returns the full shared CSS block for shadow DOM overlays:
 * font imports + custom properties + base reset.
 */
export function cxBaseCSS(): string {
    return `
        ${FONT_IMPORT_CSS}
        :host {
            ${cxVars()}
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        @media (prefers-reduced-motion: reduce) {
            *, *::before, *::after {
                animation-duration: 0.001ms !important;
                transition-duration: 0.001ms !important;
            }
        }
    `;
}

// --- Shared Keyframes for Popup/Newtab ---

export const CX_KEYFRAMES = `
    ${FONT_IMPORT_CSS}
    @keyframes cxPulse {
        0%, 100% { opacity: 1; }
        50% { opacity: 0.4; }
    }
    @keyframes cxFadeSlow {
        0%, 100% { opacity: 1; }
        50% { opacity: 0.3; }
    }
    @media (prefers-reduced-motion: reduce) {
        *, *::before, *::after {
            animation-duration: 0.001ms !important;
            transition-duration: 0.001ms !important;
        }
    }
`;
