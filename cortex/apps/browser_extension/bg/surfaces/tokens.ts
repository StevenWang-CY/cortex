/**
 * Design tokens for injected page surfaces.
 *
 * Injected functions run in the page's isolated world and cannot import
 * modules, so the worker serialises this stylesheet and passes it as an
 * argument to every ``chrome.scripting.executeScript`` call. The values are
 * read from the generated ``design-tokens.ts`` (light fallbacks embedded in
 * the ``var(--cx-*, …)`` strings, dark values from ``CX.dark``) so the page
 * panels, the popup, and the desktop shell share one palette.
 *
 * Dark-only aliases that the generator does not yet expose (accent hover,
 * accent text, subtle/emphasis borders, floating shadow) mirror
 * ``page-reset.css``; see the work-package report for the requested tokens.
 */

import { CX } from "../../design-tokens";

function fallbackOf(token: string, defaultValue: string): string {
    const match = /^var\(--[a-z0-9-]+,\s*(.+)\)$/i.exec(token.trim());
    return match ? match[1].trim() : defaultValue;
}

interface Palette {
    windowBg: string; controlBg: string; groupedBg: string;
    labelPrimary: string; labelSecondary: string; labelTertiary: string; labelInverse: string;
    borderSubtle: string; borderEmphasis: string; separator: string; shadowFloat: string;
    accent: string; accentHover: string; accentText: string;
    danger: string; success: string; warning: string; info: string;
    successText: string; warningText: string; infoText: string; dangerText: string;
}

const LIGHT: Palette = {
    windowBg: fallbackOf(CX.bg, "#ECECEC"),
    controlBg: fallbackOf(CX.surface, "#FFFFFF"),
    groupedBg: fallbackOf(CX.tertiary, "#F2F2F7"),
    labelPrimary: fallbackOf(CX.text, "#1A1A1A"),
    labelSecondary: fallbackOf(CX.textSecondary, "#5C5854"),
    labelTertiary: fallbackOf(CX.textTertiary, "#6B6661"),
    labelInverse: fallbackOf(CX.textInverse, "#FFFFFF"),
    borderSubtle: fallbackOf(CX.border, "rgba(0, 0, 0, 0.06)"),
    borderEmphasis: fallbackOf(CX.borderEmphasis, "rgba(0, 0, 0, 0.20)"),
    separator: fallbackOf(CX.borderDefault, "#3C3C4326"),
    shadowFloat: fallbackOf(
        CX.shadowFloat,
        "0 8px 32px rgba(0, 0, 0, 0.08), 0 0 0 1px rgba(0,0,0,0.04)",
    ),
    accent: fallbackOf(CX.accent, "#D97757"),
    accentHover: fallbackOf(CX.accentHover, "#C46547"),
    accentText: fallbackOf(CX.accentText, "#A4472E"),
    danger: fallbackOf(CX.danger, "#D70015"),
    success: fallbackOf(CX.success, "#30B257"),
    warning: fallbackOf(CX.warning, "#D9A100"),
    info: fallbackOf(CX.info, "#0A84FF"),
    successText: fallbackOf(CX.successText, "#1B6B3C"),
    warningText: fallbackOf(CX.warningText, "#7A5600"),
    infoText: fallbackOf(CX.infoText, "#0062CC"),
    // Mirrors page-reset.css; a generated ``danger_text`` token is requested.
    dangerText: "#B3000F",
};

const DARK: Palette = {
    windowBg: CX.dark.window_bg,
    controlBg: CX.dark.control_bg,
    groupedBg: CX.dark.grouped_bg,
    labelPrimary: CX.dark.label_primary,
    labelSecondary: CX.dark.label_secondary,
    labelTertiary: CX.dark.label_tertiary,
    labelInverse: CX.dark.label_inverse,
    borderSubtle: "rgba(255, 255, 255, 0.10)",
    borderEmphasis: "rgba(255, 255, 255, 0.24)",
    separator: CX.dark.separator,
    shadowFloat: "0 8px 32px rgba(0, 0, 0, 0.42), 0 0 0 1px rgba(255, 255, 255, 0.06)",
    accent: CX.dark.accent,
    accentHover: LIGHT.accent,
    accentText: CX.dark.accent,
    danger: CX.dark.danger,
    success: CX.dark.success,
    warning: CX.dark.warning,
    info: CX.dark.info,
    successText: CX.dark.success_text,
    warningText: CX.dark.warning_text,
    infoText: CX.dark.info_text,
    dangerText: "#FF6961",
};

function paletteDeclarations(palette: Palette): string {
    return [
        `--cx-window-bg:${palette.windowBg}`,
        `--cx-control-bg:${palette.controlBg}`,
        `--cx-grouped-bg:${palette.groupedBg}`,
        `--cx-label-primary:${palette.labelPrimary}`,
        `--cx-label-secondary:${palette.labelSecondary}`,
        `--cx-label-tertiary:${palette.labelTertiary}`,
        `--cx-label-inverse:${palette.labelInverse}`,
        `--cx-border-subtle:${palette.borderSubtle}`,
        `--cx-border-emphasis:${palette.borderEmphasis}`,
        `--cx-separator:${palette.separator}`,
        `--cx-shadow-float:${palette.shadowFloat}`,
        `--cx-accent:${palette.accent}`,
        `--cx-accent-hover:${palette.accentHover}`,
        `--cx-accent-text:${palette.accentText}`,
        `--cx-danger:${palette.danger}`,
        `--cx-success:${palette.success}`,
        `--cx-warning:${palette.warning}`,
        `--cx-info:${palette.info}`,
        `--cx-success-text:${palette.successText}`,
        `--cx-warning-text:${palette.warningText}`,
        `--cx-info-text:${palette.infoText}`,
        `--cx-danger-text:${palette.dangerText}`,
    ].join(";");
}

/**
 * ``:host`` custom properties for a shadow root: light palette by default,
 * dark palette under ``prefers-color-scheme: dark``, plus the typography,
 * radius, and motion tokens the panel stylesheet consumes.
 */
export function shadowTokensCss(): string {
    const shared = [
        `--cx-font:${CX.font}`,
        `--cx-font-serif:${CX.fontSerif}`,
        `--cx-mono:${CX.mono}`,
        `--cx-radius-window:${CX.radius.window}px`,
        `--cx-radius-card:${CX.radius.card}px`,
        `--cx-radius-control:${CX.radius.control}px`,
        `--cx-ease-out:${CX.easeOut}`,
        `--cx-ease-in-out:${CX.easeInOut}`,
        `--cx-duration-micro:${CX.motion.micro}ms`,
        `--cx-duration-fast:${CX.motion.fast}ms`,
        `--cx-duration-normal:${CX.motion.normal}ms`,
        "color-scheme:light dark",
    ].join(";");
    return `:host{${shared};${paletteDeclarations(LIGHT)}}`
        + `@media (prefers-color-scheme:dark){:host{${paletteDeclarations(DARK)}}}`;
}

/**
 * Shared class vocabulary for every injected panel. The intervention panel,
 * the LeetCode coach, and the distraction interceptor are variants of the
 * same surface: ``.cx-panel`` (corner) or ``.cx-panel--modal`` (centred, with
 * ``.cx-scrim``), a focusable ``.cx-title``, ``.cx-body`` copy, and
 * ``.cx-btn`` controls. Entrance and exit are interruptible transitions
 * driven by ``data-state`` (enter → open → exit); a replacement proposal
 * updates content in place without touching ``data-state``.
 */
export const PANEL_CSS = `
*{box-sizing:border-box;margin:0;padding:0}
.cx-layer{position:fixed;inset:0;pointer-events:none;z-index:2147483647;font-family:var(--cx-font)}
.cx-scrim{position:absolute;inset:0;background:rgba(0,0,0,.42);pointer-events:auto;opacity:0;transition:opacity var(--cx-duration-fast) var(--cx-ease-out)}
.cx-scrim[data-state="open"]{opacity:1}
.cx-center{position:absolute;inset:0;display:grid;place-items:center;pointer-events:none;padding:16px}
.cx-panel{position:relative;pointer-events:auto;background:var(--cx-control-bg);color:var(--cx-label-primary);border:1px solid var(--cx-border-subtle);border-radius:var(--cx-radius-window);box-shadow:var(--cx-shadow-float);padding:16px;font:13px/1.45 var(--cx-font);-webkit-font-smoothing:antialiased;opacity:0;transform:translateY(8px) scale(.98);transition:opacity var(--cx-duration-normal) var(--cx-ease-out),transform var(--cx-duration-normal) var(--cx-ease-out)}
.cx-panel[data-state="open"]{opacity:1;transform:none}
.cx-panel[data-state="exit"]{opacity:0;transform:translateY(8px) scale(.98);transition-duration:var(--cx-duration-fast);transition-timing-function:cubic-bezier(.4,0,1,1)}
.cx-panel--corner{position:absolute;right:20px;bottom:20px;width:340px;max-height:calc(100vh - 40px);overflow-y:auto;scrollbar-width:none}
.cx-panel--corner::-webkit-scrollbar{width:0}
.cx-panel--modal{width:min(380px,calc(100vw - 32px));padding:24px 22px 20px;text-align:center}
.cx-title{font-size:15px;font-weight:600;line-height:1.35;letter-spacing:-.01em;padding-right:28px;outline:none;border-radius:4px}
.cx-panel--modal .cx-title{padding-right:0}
.cx-title:focus-visible{outline:2px solid var(--cx-accent);outline-offset:3px}
.cx-body{margin-top:4px;font-size:13px;line-height:1.5;color:var(--cx-label-secondary)}
.cx-body strong{font-weight:600;color:var(--cx-label-primary)}
.cx-section{margin-top:12px;padding-top:12px;border-top:1px solid var(--cx-border-subtle)}
.cx-label{margin-bottom:6px;font-size:11px;font-weight:600;letter-spacing:.02em;text-transform:uppercase;color:var(--cx-label-secondary)}
.cx-item{display:flex;align-items:flex-start;gap:8px;padding:4px 0;font-size:13px;line-height:1.45;color:var(--cx-label-primary)}
.cx-item::before{content:"";flex:0 0 auto;width:4px;height:4px;margin-top:8px;border-radius:50%;background:var(--cx-label-tertiary)}
.cx-item.is-done{color:var(--cx-label-secondary);text-decoration:line-through}
.cx-item .cx-sub{display:block;font-size:12px;color:var(--cx-label-tertiary);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cx-item .cx-text{min-width:0;overflow:hidden;text-overflow:ellipsis}
.cx-keep{margin-top:6px;font-size:12px;color:var(--cx-label-tertiary)}
.cx-keep b{font-weight:600;color:var(--cx-success-text)}
.cx-note{margin-top:12px;padding:8px 10px;border-radius:var(--cx-radius-card);background:var(--cx-grouped-bg);font-size:12px;line-height:1.45;color:var(--cx-label-secondary)}
.cx-error{margin-top:12px;padding:10px 12px;border-radius:var(--cx-radius-card);background:color-mix(in srgb,var(--cx-danger) 10%,transparent);border:1px solid color-mix(in srgb,var(--cx-danger) 30%,transparent)}
.cx-error-head{margin-bottom:4px;font-size:11px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--cx-danger-text)}
.cx-error-text{font-size:13px;line-height:1.5;color:var(--cx-label-primary)}
.cx-code{margin-top:6px;padding:8px;border-radius:var(--cx-radius-control);background:var(--cx-grouped-bg);font:12px/1.5 var(--cx-mono);white-space:pre-wrap;color:var(--cx-label-secondary)}
.cx-btn{appearance:none;display:inline-flex;align-items:center;justify-content:center;gap:6px;min-height:36px;padding:8px 14px;border:1px solid transparent;border-radius:var(--cx-radius-control);background:transparent;color:var(--cx-label-primary);font:500 13px/1.2 var(--cx-font);cursor:pointer;transition:background-color var(--cx-duration-micro) var(--cx-ease-out),color var(--cx-duration-micro) var(--cx-ease-out),border-color var(--cx-duration-micro) var(--cx-ease-out),opacity var(--cx-duration-micro) var(--cx-ease-out),transform var(--cx-duration-micro) var(--cx-ease-out)}
.cx-btn:active:not(:disabled){transform:scale(.97)}
.cx-btn:focus-visible,.cx-close:focus-visible,.cx-link:focus-visible,input:focus-visible{outline:2px solid var(--cx-accent);outline-offset:2px}
.cx-btn:disabled{cursor:default}
.cx-btn--primary{width:100%;background:var(--cx-label-primary);color:var(--cx-label-inverse)}
.cx-btn--ghost{border-color:var(--cx-border-emphasis)}
.cx-btn--quiet{color:var(--cx-label-secondary)}
.cx-btn--danger{background:color-mix(in srgb,var(--cx-danger) 12%,transparent);color:var(--cx-danger-text);border-color:color-mix(in srgb,var(--cx-danger) 36%,transparent)}
.cx-btn[data-phase="pending"]{opacity:.7}
.cx-btn[data-phase="applied"],.cx-btn[data-phase="partial"]{background:color-mix(in srgb,var(--cx-success) 16%,transparent);color:var(--cx-success-text);border-color:color-mix(in srgb,var(--cx-success) 40%,transparent)}
.cx-btn[data-phase="failed"]{background:color-mix(in srgb,var(--cx-danger) 12%,transparent);color:var(--cx-danger-text);border-color:color-mix(in srgb,var(--cx-danger) 36%,transparent);white-space:normal;line-height:1.35;text-align:left;justify-content:flex-start}
.cx-btn[data-phase="restored"]{background:var(--cx-grouped-bg);color:var(--cx-label-secondary)}
.cx-actions{display:flex;flex-wrap:wrap;align-items:center;justify-content:center;gap:8px;margin-top:14px}
.cx-actions--stack{flex-direction:column;align-items:stretch}
.cx-link{background:none;border:0;padding:4px 2px;border-radius:4px;color:var(--cx-accent-text);font:500 12px var(--cx-font);cursor:pointer;text-decoration:underline;text-underline-offset:2px}
.cx-status{margin-top:8px;font-size:12px;line-height:1.4;text-align:center;color:var(--cx-label-secondary);min-height:1.4em}
.cx-close{position:absolute;top:8px;right:8px;display:grid;place-items:center;width:32px;height:32px;border:0;border-radius:var(--cx-radius-control);background:transparent;color:var(--cx-label-secondary);cursor:pointer;transition:background-color var(--cx-duration-micro) var(--cx-ease-out),color var(--cx-duration-micro) var(--cx-ease-out),transform var(--cx-duration-micro) var(--cx-ease-out)}
.cx-close:active{transform:scale(.96)}
.cx-close svg{width:10px;height:10px;stroke:currentColor;stroke-width:2;fill:none}
.cx-tags{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.cx-tag{padding:3px 8px;border-radius:9999px;border:1px solid color-mix(in srgb,var(--cx-accent) 32%,transparent);background:color-mix(in srgb,var(--cx-accent) 10%,transparent);font-size:11px;font-weight:500;color:var(--cx-accent-text)}
.cx-hint{margin-top:10px;padding:10px;border-radius:var(--cx-radius-card);background:var(--cx-grouped-bg);font-size:13px;line-height:1.45;color:var(--cx-label-primary)}
.cx-check{display:flex;align-items:center;gap:8px;margin-top:12px;font-size:13px;color:var(--cx-label-primary);cursor:pointer}
.cx-check input{width:16px;height:16px;accent-color:var(--cx-accent)}
.cx-meta{font:11px var(--cx-mono);letter-spacing:.02em;color:var(--cx-label-tertiary)}
.cx-hero{margin:14px 0 4px;font:600 32px/1.1 var(--cx-font-serif);font-variant-numeric:tabular-nums;color:var(--cx-label-primary)}
.cx-dot{display:inline-block;width:8px;height:8px;margin-right:8px;border-radius:50%;background:var(--cx-accent);vertical-align:middle}
@media (hover:hover) and (pointer:fine){
.cx-btn--primary:hover:not(:disabled){box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--cx-label-inverse) 18%,transparent)}
.cx-btn--ghost:hover:not(:disabled),.cx-btn--quiet:hover:not(:disabled){background:var(--cx-grouped-bg);color:var(--cx-label-primary)}
.cx-close:hover{background:var(--cx-grouped-bg);color:var(--cx-label-primary)}
.cx-link:hover{color:var(--cx-accent-hover)}
}
@media (prefers-reduced-motion:reduce){
.cx-panel,.cx-scrim{transition:opacity var(--cx-duration-fast) var(--cx-ease-out)!important;transform:none!important}
.cx-btn,.cx-close{transition-property:background-color,color,border-color,opacity!important}
.cx-btn:active:not(:disabled),.cx-close:active{transform:none}
}
@media (prefers-reduced-transparency:reduce){
.cx-scrim{background:rgba(0,0,0,.6)}
}
`;

/** Complete stylesheet for one injected surface. */
export function surfaceCss(): string {
    return shadowTokensCss() + PANEL_CSS;
}
