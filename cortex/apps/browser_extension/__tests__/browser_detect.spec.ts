/**
 * Browser detection — drives the WebSocket IDENTIFY ``client_type`` so
 * the desktop dashboard's Chrome/Edge connection dots light up the
 * right indicator. Pre-fix the extension shipped a hardcoded
 * ``client_type: "chrome"`` for both browsers, so an Edge install
 * always reported as Chrome.
 *
 * The daemon rejects unknown ``client_type`` literals (see
 * cortex/services/api_gateway/websocket_server.py:480) — that allowlist
 * is ``{chrome, edge, vscode, desktop}``. Anything else is dropped to
 * ``"unknown"`` and the dashboard never sees it.
 */

import { describe, expect, it } from "vitest";

import { detectBrowser } from "../lib/browser";

describe("detectBrowser", () => {
    it("returns 'edge' when userAgentData reports the Microsoft Edge brand", () => {
        const nav = {
            userAgentData: {
                brands: [
                    { brand: "Not_A Brand", version: "8" },
                    { brand: "Chromium", version: "120" },
                    { brand: "Microsoft Edge", version: "120" },
                ],
            },
        };
        expect(detectBrowser(nav)).toBe("edge");
    });

    it("returns 'chrome' when userAgentData reports only Chromium + Google Chrome", () => {
        const nav = {
            userAgentData: {
                brands: [
                    { brand: "Not_A Brand", version: "8" },
                    { brand: "Chromium", version: "120" },
                    { brand: "Google Chrome", version: "120" },
                ],
            },
        };
        expect(detectBrowser(nav)).toBe("chrome");
    });

    it("returns 'edge' when the userAgent string contains the ``Edg/`` token", () => {
        const nav = {
            userAgent:
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) " +
                "AppleWebKit/537.36 (KHTML, like Gecko) " +
                "Chrome/120.0.0.0 Safari/537.36 Edg/120.0.2210.121",
        };
        expect(detectBrowser(nav)).toBe("edge");
    });

    it("returns 'edge' for the legacy Edge mobile / iOS variants", () => {
        expect(detectBrowser({ userAgent: "EdgA/120.0.0.0 Mobile" })).toBe(
            "edge",
        );
        expect(detectBrowser({ userAgent: "EdgiOS/120.0.0.0 iPhone" })).toBe(
            "edge",
        );
    });

    it("returns 'chrome' for a plain Chrome userAgent without Edg token", () => {
        const nav = {
            userAgent:
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) " +
                "AppleWebKit/537.36 (KHTML, like Gecko) " +
                "Chrome/120.0.0.0 Safari/537.36",
        };
        expect(detectBrowser(nav)).toBe("chrome");
    });

    it("falls back to 'chrome' when neither signal is available", () => {
        expect(detectBrowser({})).toBe("chrome");
    });

    it("prefers UA-CH over userAgent when both disagree (UA-CH is more reliable)", () => {
        // Chromium prior to UA-reduction sometimes shipped a generic
        // Chrome userAgent on Edge. The UA-CH brand list is the
        // authoritative source on modern Edge.
        const nav = {
            userAgentData: {
                brands: [{ brand: "Microsoft Edge", version: "120" }],
            },
            userAgent:
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) " +
                "AppleWebKit/537.36 (KHTML, like Gecko) " +
                "Chrome/120.0.0.0 Safari/537.36",
        };
        expect(detectBrowser(nav)).toBe("edge");
    });

    it("does not throw when navigator access raises (sandboxed SW context)", () => {
        // Pass a navigator-like object whose property accessors throw —
        // simulates an unusual sandboxed environment. The helper must
        // still return a valid CortexBrowser literal.
        const exploding = new Proxy(
            {},
            {
                get() {
                    throw new Error("sandboxed");
                },
            },
        );
        expect(detectBrowser(exploding as never)).toBe("chrome");
    });
});
