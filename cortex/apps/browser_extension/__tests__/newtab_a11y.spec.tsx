/** Decorative pulse art must not invent a breathing instruction. */

import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { beforeEach, describe, expect, it, vi } from "vitest";

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

    it("keeps decorative pulse art out of the accessibility tree", async () => {
        const { container, cleanup } = await renderNewtab();
        try {
            const canvas = container.querySelector("canvas");
            expect(canvas?.getAttribute("aria-hidden")).toBe("true");
            expect(canvas?.hasAttribute("role")).toBe(false);
            expect(container.querySelector('[data-testid="pacer-phase-announcement"]')).toBeNull();
            expect(container.querySelector('[role="status"]')?.textContent).toBe("Cortex is resting");
        } finally { await cleanup(); }
    });

    it("exposes recent activities as a real list of navigable links", async () => {
        const fake = globalThis.__cortexChrome;
        (fake.runtime.sendMessage as ReturnType<typeof import("vitest").vi.fn>)
            .mockImplementation(
                (msg: Record<string, unknown>, cb?: (r: unknown) => void) => {
                    if (msg.type === "GET_RECENT_ACTIVITIES") {
                        cb?.([{
                            content_id: "activity-1",
                            platform: "web",
                            content_type: "article",
                            title: "Continue the architecture review",
                            url: "https://example.com/review",
                            position: {},
                            content_duration_s: 600,
                            duration_spent_s: 120,
                            last_visited: 1,
                            completion_pct: 20,
                            max_completion_pct: 20,
                            related_tabs: [],
                        }]);
                        return;
                    }
                    cb?.({ connected: false, state: null });
                },
            );

        const { container, cleanup } = await renderNewtab();
        try {
            const list = container.querySelector('[role="list"][aria-label="Recent activities"]');
            const item = list?.querySelector('[role="listitem"]');
            const link = item?.querySelector("a.cortex-resume-card");
            expect(list).not.toBeNull();
            expect(item).not.toBeNull();
            expect(link?.getAttribute("href")).toBe("https://example.com/review");
            expect(link?.classList.contains("cortex-translucent-material")).toBe(true);
        } finally {
            await cleanup();
        }
    });

    it("provides an opaque fallback for translucent new-tab materials", async () => {
        const { container, cleanup } = await renderNewtab();
        try {
            const styles = document.getElementById("cortex-newtab-styles")?.textContent ?? "";
            const launchButton = container.querySelector(".cortex-launch-button");
            expect(styles).toContain("prefers-reduced-transparency: reduce");
            expect(styles).toContain("backdrop-filter: none !important");
            expect(launchButton?.classList.contains("cortex-translucent-material")).toBe(true);
        } finally {
            await cleanup();
        }
    });

    it("honors Reduce Motion before the first canvas effect", async () => {
        const requestFrame = vi.fn(() => 1);
        const cancelFrame = vi.fn();
        vi.stubGlobal("requestAnimationFrame", requestFrame);
        vi.stubGlobal("cancelAnimationFrame", cancelFrame);
        vi.stubGlobal(
            "matchMedia",
            vi.fn((query: string) => ({
                matches: query === "(prefers-reduced-motion: reduce)",
                media: query,
                onchange: null,
                addListener: vi.fn(),
                removeListener: vi.fn(),
                addEventListener: vi.fn(),
                removeEventListener: vi.fn(),
                dispatchEvent: vi.fn(() => false),
            })),
        );

        const { cleanup } = await renderNewtab();
        try {
            expect(requestFrame).not.toHaveBeenCalled();
            expect(cancelFrame).not.toHaveBeenCalled();
        } finally {
            await cleanup();
            vi.unstubAllGlobals();
        }
    });

    it("stops the canvas loop while hidden and resumes exactly once", async () => {
        const originalVisibility = Object.getOwnPropertyDescriptor(
            document,
            "visibilityState",
        );
        let visibility: DocumentVisibilityState = "visible";
        let nextFrameId = 1;
        const callbacks = new Map<number, FrameRequestCallback>();
        const requestFrame = vi.fn((callback: FrameRequestCallback) => {
            const id = nextFrameId++;
            callbacks.set(id, callback);
            return id;
        });
        const cancelFrame = vi.fn((id: number) => {
            callbacks.delete(id);
        });
        vi.stubGlobal("requestAnimationFrame", requestFrame);
        vi.stubGlobal("cancelAnimationFrame", cancelFrame);
        Object.defineProperty(document, "visibilityState", {
            configurable: true,
            get: () => visibility,
        });

        const { cleanup } = await renderNewtab();
        try {
            expect(requestFrame).toHaveBeenCalledTimes(1);
            expect(callbacks.size).toBe(1);

            visibility = "hidden";
            await act(async () => {
                document.dispatchEvent(new Event("visibilitychange"));
            });
            expect(cancelFrame).toHaveBeenCalledTimes(1);
            expect(callbacks.size).toBe(0);

            visibility = "visible";
            await act(async () => {
                document.dispatchEvent(new Event("visibilitychange"));
                document.dispatchEvent(new Event("visibilitychange"));
            });
            expect(requestFrame).toHaveBeenCalledTimes(2);
            expect(callbacks.size).toBe(1);
        } finally {
            await cleanup();
            vi.unstubAllGlobals();
            if (originalVisibility) {
                Object.defineProperty(
                    document,
                    "visibilityState",
                    originalVisibility,
                );
            } else {
                delete (
                    document as unknown as Record<string, unknown>
                ).visibilityState;
            }
        }
    });
});
