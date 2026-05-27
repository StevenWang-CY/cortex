/**
 * P2-7 — newtab canvas accessibility.
 *
 * The PulseRoom canvas MUST:
 *   - carry ``role="img"`` (WCAG SC 1.1.1 non-text content),
 *   - carry ``aria-label="Cortex breathing pacer visualization"``
 *     (human-readable description of the visualisation),
 *   - be accompanied by an ``aria-live="polite"`` region
 *     (``data-testid="pacer-phase-announcement"``) that announces the
 *     current breathing phase to screen reader users.
 */

import React from "react";
import { createRoot } from "react-dom/client";
import { act } from "react-dom/test-utils";
import { describe, expect, it, beforeEach } from "vitest";

// PulseRoom is the default export of newtab.tsx.
import PulseRoom from "../newtab";

async function renderNewtab(): Promise<{
    container: HTMLDivElement;
    cleanup: () => Promise<void>;
}> {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    await act(async () => {
        root.render(React.createElement(PulseRoom));
    });
    await act(async () => {
        await new Promise((r) => setTimeout(r, 0));
    });
    return {
        container,
        cleanup: async () => {
            await act(async () => { root.unmount(); });
            container.remove();
        },
    };
}

describe("newtab canvas accessibility (P2-7)", () => {
    beforeEach(() => {
        // Stub chrome.runtime.sendMessage so the component doesn't throw.
        const fake = globalThis.__cortexChrome;
        (fake.runtime.sendMessage as ReturnType<typeof import("vitest").vi.fn>)
            .mockImplementation(
                (_msg: unknown, cb?: (r: unknown) => void) => {
                    cb?.({ connected: false, state: null });
                },
            );
    });

    it("canvas has role='img'", async () => {
        const { container, cleanup } = await renderNewtab();
        try {
            const canvas = container.querySelector("canvas");
            expect(canvas).not.toBeNull();
            expect(canvas?.getAttribute("role")).toBe("img");
        } finally {
            await cleanup();
        }
    });

    it("canvas has aria-label describing the visualisation", async () => {
        const { container, cleanup } = await renderNewtab();
        try {
            const canvas = container.querySelector("canvas");
            expect(canvas?.getAttribute("aria-label")).toBe(
                "Cortex breathing pacer visualization",
            );
        } finally {
            await cleanup();
        }
    });

    it("aria-live region for pacer phase is present", async () => {
        const { container, cleanup } = await renderNewtab();
        try {
            const liveRegion = container.querySelector(
                '[data-testid="pacer-phase-announcement"]',
            );
            expect(liveRegion).not.toBeNull();
            expect(liveRegion?.getAttribute("aria-live")).toBe("polite");
        } finally {
            await cleanup();
        }
    });
});
