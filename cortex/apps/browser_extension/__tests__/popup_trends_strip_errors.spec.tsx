/**
 * P0 §3.2 — TrendsMiniStrip renders a "temporarily unavailable" copy
 * when the chrome.runtime.sendMessage callback fails.
 *
 * The previous strip rendered the generic "Not enough data yet" empty
 * state on any non-success — including SW eviction and port-disconnect
 * failures — which lied about the cause. The hardened strip now sets a
 * dedicated ``loadFailed`` flag when:
 *   - ``chrome.runtime.lastError`` populates in the GET_CACHED_TRENDS
 *     callback (port disconnected mid-call),
 *   - the GET_CACHED_TRENDS / REQUEST_TRENDS sendMessage call throws,
 *
 * and renders "Trends temporarily unavailable" in place of the empty
 * state until a subsequent successful response or TRENDS_READY
 * broadcast resets the flag.
 */

import React from "react";
import { createRoot } from "react-dom/client";
import { act } from "react-dom/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import CortexPopup from "../popup";

type SendMessageResponder = (
    msg: Record<string, unknown>,
    cb?: (resp: unknown) => void,
) => unknown;

interface Harness {
    container: HTMLDivElement;
    cleanup: () => Promise<void>;
}

function installResponder(responder: SendMessageResponder): ReturnType<typeof vi.fn> {
    const fake = globalThis.__cortexChrome;
    const fn = vi.fn(
        (msg: Record<string, unknown>, cb?: (resp: unknown) => void) => {
            const out = responder(msg, cb);
            return Promise.resolve(out);
        },
    );
    fake.runtime.sendMessage = fn as unknown as typeof fake.runtime.sendMessage;
    return fn;
}

async function renderPopup(): Promise<Harness> {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    await act(async () => {
        root.render(React.createElement(CortexPopup));
    });
    // Let the mount-time sendMessage callbacks resolve.
    await act(async () => {
        await new Promise((r) => setTimeout(r, 0));
    });
    const cleanup = async () => {
        await act(async () => {
            root.unmount();
        });
        container.remove();
    };
    return { container, cleanup };
}

describe("TrendsMiniStrip — load-failure error UI", () => {
    beforeEach(() => {
        globalThis.__cortexChrome.storage.local.__reset({});
        globalThis.__cortexChrome.runtime.lastError = undefined;
    });

    afterEach(() => {
        globalThis.__cortexChrome.runtime.lastError = undefined;
        vi.useRealTimers();
    });

    it("renders the error copy when chrome.runtime.lastError populates", async () => {
        installResponder((msg, cb) => {
            // Default fan-out for non-trends messages.
            if (msg.type === "GET_STATE") {
                cb?.({
                    connected: false,
                    state: null,
                    intervention: null,
                    focusSession: null,
                });
                return undefined;
            }
            if (msg.type === "GET_DAILY_STATS") {
                cb?.(null);
                return undefined;
            }
            if (msg.type === "GET_CACHED_RECAP") {
                cb?.({ recap: null, timestamp: null });
                return undefined;
            }
            if (msg.type === "GET_CACHED_TRENDS") {
                // Simulate the MV3 "port disconnected" failure path.
                globalThis.__cortexChrome.runtime.lastError = {
                    message: "The message port closed before a response was received.",
                };
                cb?.(undefined);
                globalThis.__cortexChrome.runtime.lastError = undefined;
                return undefined;
            }
            cb?.(undefined);
            return undefined;
        });

        const { container, cleanup } = await renderPopup();
        try {
            const errEl = container.querySelector(
                '[data-testid="trends-error"]',
            );
            expect(errEl).not.toBeNull();
            expect(errEl?.textContent ?? "").toMatch(/temporarily unavailable/i);
            // Bars and empty-state must NOT also be rendered.
            expect(
                container.querySelector('[data-testid="trends-empty"]'),
            ).toBeNull();
            expect(
                container.querySelectorAll('[data-testid^="trends-bar-"]').length,
            ).toBe(0);
        } finally {
            await cleanup();
        }
    });

    it("renders the error copy when GET_CACHED_TRENDS sendMessage throws", async () => {
        installResponder((msg, cb) => {
            if (msg.type === "GET_STATE") {
                cb?.({
                    connected: false,
                    state: null,
                    intervention: null,
                    focusSession: null,
                });
                return undefined;
            }
            if (msg.type === "GET_DAILY_STATS") {
                cb?.(null);
                return undefined;
            }
            if (msg.type === "GET_CACHED_RECAP") {
                cb?.({ recap: null, timestamp: null });
                return undefined;
            }
            if (msg.type === "GET_CACHED_TRENDS") {
                // Synchronous throw — exercises the outer try/catch
                // around the entire GET_CACHED_TRENDS sendMessage call.
                throw new Error("Extension context invalidated.");
            }
            cb?.(undefined);
            return undefined;
        });

        const { container, cleanup } = await renderPopup();
        try {
            const errEl = container.querySelector(
                '[data-testid="trends-error"]',
            );
            expect(errEl).not.toBeNull();
            expect(errEl?.textContent ?? "").toMatch(/temporarily unavailable/i);
        } finally {
            await cleanup();
        }
    });

    it("renders the error copy with role=status for screen readers", async () => {
        installResponder((msg, cb) => {
            if (msg.type === "GET_STATE") {
                cb?.({
                    connected: false,
                    state: null,
                    intervention: null,
                    focusSession: null,
                });
                return undefined;
            }
            if (msg.type === "GET_DAILY_STATS") {
                cb?.(null);
                return undefined;
            }
            if (msg.type === "GET_CACHED_RECAP") {
                cb?.({ recap: null, timestamp: null });
                return undefined;
            }
            if (msg.type === "GET_CACHED_TRENDS") {
                globalThis.__cortexChrome.runtime.lastError = {
                    message: "port closed",
                };
                cb?.(undefined);
                globalThis.__cortexChrome.runtime.lastError = undefined;
                return undefined;
            }
            cb?.(undefined);
            return undefined;
        });

        const { container, cleanup } = await renderPopup();
        try {
            const errEl = container.querySelector(
                '[data-testid="trends-error"]',
            );
            expect(errEl).not.toBeNull();
            expect(errEl?.getAttribute("role")).toBe("status");
            expect(errEl?.getAttribute("aria-live")).toBe("polite");
        } finally {
            await cleanup();
        }
    });

    it("does NOT render the error copy when GET_CACHED_TRENDS succeeds", async () => {
        installResponder((msg, cb) => {
            if (msg.type === "GET_STATE") {
                cb?.({
                    connected: false,
                    state: null,
                    intervention: null,
                    focusSession: null,
                });
                return undefined;
            }
            if (msg.type === "GET_DAILY_STATS") {
                cb?.(null);
                return undefined;
            }
            if (msg.type === "GET_CACHED_RECAP") {
                cb?.({ recap: null, timestamp: null });
                return undefined;
            }
            if (msg.type === "GET_CACHED_TRENDS") {
                cb?.({ trends: null, timestamp: null });
                return undefined;
            }
            if (msg.type === "REQUEST_TRENDS") {
                cb?.({ trends: null, timestamp: null });
                return undefined;
            }
            cb?.(undefined);
            return undefined;
        });

        const { container, cleanup } = await renderPopup();
        try {
            // Success path lands in the empty state (no data), NOT
            // the error state.
            expect(
                container.querySelector('[data-testid="trends-error"]'),
            ).toBeNull();
            expect(
                container.querySelector('[data-testid="trends-empty"]'),
            ).not.toBeNull();
        } finally {
            await cleanup();
        }
    });

    it("prefers cached bars over the error copy when GET_CACHED_TRENDS succeeded earlier", async () => {
        // GET_CACHED_TRENDS returns valid data; REQUEST_TRENDS fails.
        // The strip should still render the bars (stale data > no data).
        installResponder((msg, cb) => {
            if (msg.type === "GET_STATE") {
                cb?.({
                    connected: false,
                    state: null,
                    intervention: null,
                    focusSession: null,
                });
                return undefined;
            }
            if (msg.type === "GET_DAILY_STATS") {
                cb?.(null);
                return undefined;
            }
            if (msg.type === "GET_CACHED_RECAP") {
                cb?.({ recap: null, timestamp: null });
                return undefined;
            }
            if (msg.type === "GET_CACHED_TRENDS") {
                // Stale-but-present payload (7h old → triggers the
                // REQUEST_TRENDS nudge below).
                cb?.({
                    trends: {
                        window: "week",
                        daily: [
                            { record_date: "2026-05-19", total_flow_minutes: 12 },
                            { record_date: "2026-05-20", total_flow_minutes: 30 },
                            { record_date: "2026-05-21", total_flow_minutes: 8 },
                        ],
                    },
                    timestamp: Date.now() - 7 * 60 * 60 * 1000,
                });
                return undefined;
            }
            if (msg.type === "REQUEST_TRENDS") {
                globalThis.__cortexChrome.runtime.lastError = {
                    message: "port closed mid-refresh",
                };
                cb?.(undefined);
                globalThis.__cortexChrome.runtime.lastError = undefined;
                return undefined;
            }
            cb?.(undefined);
            return undefined;
        });

        const { container, cleanup } = await renderPopup();
        try {
            // Bars should still render — the cache supplied real data.
            const bars = container.querySelectorAll(
                '[data-testid^="trends-bar-"]',
            );
            expect(bars.length).toBe(3);
            // Error UI must NOT clobber the bars.
            expect(
                container.querySelector('[data-testid="trends-error"]'),
            ).toBeNull();
        } finally {
            await cleanup();
        }
    });
});
