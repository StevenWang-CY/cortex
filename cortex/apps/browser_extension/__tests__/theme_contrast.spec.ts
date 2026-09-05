/**
 * Resolves every ``--cx-*`` variable from page-reset.css under both
 * ``prefers-color-scheme`` values in jsdom and asserts WCAG AA (>= 4.5:1)
 * for every text/background pair the popup's buttons and status controls
 * use. A token regression in either palette fails here, not in a screenshot.
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { CX } from "../design-tokens";
import { shadowTokensCss } from "../bg/surfaces/tokens";

const testDir = path.dirname(fileURLToPath(import.meta.url));
const css = fs.readFileSync(path.resolve(testDir, "../page-reset.css"), "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, "");

type Palette = Map<string, string>;

function declarations(block: string): Palette {
    const out: Palette = new Map();
    for (const match of block.matchAll(/(--cx-[a-z0-9-]+)\s*:\s*([^;]+);/g)) {
        out.set(match[1], match[2].replace(/\s+/g, " ").trim());
    }
    return out;
}

function palettes(source: string): { light: Palette; dark: Palette } {
    const light = declarations(/:root\s*\{([^}]*)\}/.exec(source)?.[1] ?? "");
    const darkBlock = /@media\s*\(prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{([^}]*)\}/.exec(source)?.[1] ?? "";
    const dark = new Map(light);
    for (const [key, value] of declarations(darkBlock)) dark.set(key, value);
    return { light, dark };
}

/** Apply a palette to the document and read it back through the CSSOM. */
function resolve(palette: Palette, name: string): string {
    const root = document.documentElement;
    for (const [key, value] of palette) root.style.setProperty(key, value);
    const resolved = getComputedStyle(root).getPropertyValue(name).trim();
    return resolved || palette.get(name) || "";
}

type Rgba = [number, number, number, number];

function parseColor(value: string): Rgba {
    const hex = /^#([0-9a-f]{3,8})$/i.exec(value.trim());
    if (hex) {
        let h = hex[1];
        if (h.length === 3 || h.length === 4) h = h.split("").map((c) => c + c).join("");
        const n = parseInt(h.slice(0, 6), 16);
        const alpha = h.length === 8 ? parseInt(h.slice(6, 8), 16) / 255 : 1;
        return [(n >> 16) & 255, (n >> 8) & 255, n & 255, alpha];
    }
    const rgb = /^rgba?\(([^)]+)\)$/i.exec(value.trim());
    if (rgb) {
        const parts = rgb[1].split(",").map((p) => parseFloat(p.trim()));
        return [parts[0], parts[1], parts[2], parts.length > 3 ? parts[3] : 1];
    }
    throw new Error(`unparseable colour ${value}`);
}

function composite(fg: Rgba, bg: Rgba): Rgba {
    const a = fg[3];
    return [
        fg[0] * a + bg[0] * (1 - a),
        fg[1] * a + bg[1] * (1 - a),
        fg[2] * a + bg[2] * (1 - a),
        1,
    ];
}

function luminance([r, g, b]: Rgba): number {
    const lin = (c: number) => {
        const s = c / 255;
        return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
    };
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

function contrast(text: Rgba, background: Rgba): number {
    const fg = composite(text, background);
    const [l1, l2] = [luminance(fg), luminance(background)].sort((a, b) => b - a);
    return (l1 + 0.05) / (l2 + 0.05);
}

/** [text token, background token, where it is used]. */
const BUTTON_PAIRS: Array<[string, string, string]> = [
    ["--cx-label-inverse", "--cx-label-primary", "primary CTA / bug-report submit"],
    ["--cx-label-primary", "--cx-control-bg", "ghost buttons"],
    ["--cx-label-secondary", "--cx-control-bg", "quiet / segmented controls"],
    ["--cx-accent-text", "--cx-control-bg", "links, Helpful rating, undo"],
    ["--cx-success-text", "--cx-control-bg", "Applied state"],
    ["--cx-danger-text", "--cx-control-bg", "Nothing changed state / Not helpful"],
    ["--cx-label-inverse", "--cx-danger", "Stop confirm"],
    ["--cx-label-tertiary", "--cx-window-bg", "captions"],
    ["--cx-label-primary", "--cx-grouped-bg", "status notes"],
];

describe("token contrast contract (light + dark)", () => {
    const { light, dark } = palettes(css);

    it("resolves the variables under both schemes", () => {
        expect(light.size).toBeGreaterThan(20);
        expect(dark.get("--cx-control-bg")).not.toBe(light.get("--cx-control-bg"));
        expect(resolve(light, "--cx-label-inverse")).toBe("#FFFFFF");
        expect(resolve(dark, "--cx-label-inverse")).toBe("#1A1A1A");
    });

    for (const [scheme, palette] of [["light", light], ["dark", dark]] as const) {
        it(`keeps every button text/background pair at or above 4.5:1 in ${scheme} mode`, () => {
            const failures: string[] = [];
            const controlBg = parseColor(resolve(palette, "--cx-control-bg"));
            for (const [text, background, use] of BUTTON_PAIRS) {
                const textColor = parseColor(resolve(palette, text));
                const bgColor = composite(parseColor(resolve(palette, background)), controlBg);
                const ratio = contrast(textColor, bgColor);
                if (ratio < 4.5) failures.push(`${use}: ${text} on ${background} = ${ratio.toFixed(2)}`);
            }
            expect(failures).toEqual([]);
        });
    }

    it("wires the generated tokens to the same variables the popup uses", () => {
        expect(CX.textInverse).toContain("--cx-label-inverse");
        expect(CX.successText).toContain("--cx-success-text");
        const shadow = shadowTokensCss();
        expect(shadow).toContain("--cx-label-inverse:#FFFFFF");
        expect(shadow).toContain("prefers-color-scheme:dark");
        expect(shadow).toContain("--cx-label-inverse:#1A1A1A");
        expect(shadow).toContain("--cx-danger-text:#FF6961");
    });
});
