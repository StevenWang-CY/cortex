/**
 * P2-4 — TrendsMiniStrip stale-data badge.
 *
 * When the background script cannot be reached (loadFailed=true) but
 * the popup has a cached ``TrendsResponse`` from a prior successful
 * call, the strip MUST:
 *   - keep the existing bars visible (not swap to the error copy),
 *   - render a ``[data-testid="trends-stale-badge"]`` element next to
 *     the strip header to signal that the data may be outdated.
 *
 * Inverse: when loadFailed is false the badge MUST NOT be present.
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

const FAKE_TRENDS = {
    window: "week" as const,
    daily: [
        { record_date: "2026-05-19", total_flow_minutes: 10 },
        { record_date: "2026-05-20", total_flow_minutes: 22 },
        { record_date: "2026-05-21", total_flow_minutes: 15 },
        { record_date: "2026-05-22", total_flow_minutes: 8 },
        { record_date: "2026-05-23", total_flow_minutes: 30 },
        { record_date: "2026-05-24", total_flow_minutes: 45 },
        { record_date: "2026-05-25", total_flow_minutes: 18 },
    ],
    last_aggregated: "2026-05-25T08:00:00Z",
};

async function renderPopup(): Promise<{
    container: HTMLDivElement;
    cleanup: () => Promise<void>;
}> {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    await act(async () => {
        root.render(React.createElement(CortexPopup));
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

function defaultResponder(overrides: Record<string, unknown> = {}): SendMessageResponder {
    return (msg, cb) => {
        switch (msg.type) {
            case "GET_STATE":
                cb?.({ connected: false, state: null, intervention: null, focusSession: null });
                return undefined;
            case "GET_DAILY_STATS":
                cb?.(null);
                return undefined;
            case "GET_CACHED_RECAP":
                cb?.({ recap: null, timestamp: null });
                return undefined;
            case "GET_CACHED_TRENDS":
                cb?.(overrides.GET_CACHED_TRENDS ?? { trends: null, timestamp: null });
                return undefined;
            case "REQUEST_TRENDS":
                cb?.(overrides.REQUEST_TRENDS ?? { trends: null, timestamp: null });
                return undefined;
            default:
                cb?.(undefined);
                return undefined;
        }
    };
}

describe("TrendsMiniStrip stale badge (P2-4)", () => {
    beforeEach(() => {
        globalThis.__cortexChrome.storage.local.__reset({});
    });

    afterEach(() => {
        vi.useRealTimers();
    });

    it("shows the stale badge when loadFailed=true and cached trends exist", async () => {
        // First call (GET_CACHED_TRENDS) succeeds with data,
        // second call (REQUEST_TRENDS) fails → loadFailed=true but trends!=null.
        let callCount = 0;
        installResponder((msg, cb) => {
            switch (msg.type) {
                case "GET_STATE":
                    cb?.({ connected: false, state: null, intervention: null, focusSession: null });
                    return undefined;
                case "GET_DAILY_STATS":
                    cb?.(null);
                    return undefined;
                case "GET_CACHED_RECAP":
                    cb?.({ recap: null, timestamp: null });
                    return undefined;
                case "GET_CACHED_TRENDS": {
                    // Return stale cached data so loadFailed starts false but stale=true
                    // triggers REQUEST_TRENDS.
                    callCount++;
                    cb?.({
                        trends: FAKE_TRENDS,
                        // Timestamp older than TRENDS_STALENESS_MS (6 hours) to trigger refresh
                        timestamp: Date.now() - 7 * 60 * 60 * 1000,
                    });
                    return undefined;
                }
                case "REQUEST_TRENDS": {
                    // Simulate refresh failure by calling cb with lastError
                    callCount++;
                    // Simulate failure by not calling cb (port closed simulation):
                    // The safeSendMessage wrapper will invoke the lastError path.
                    // Here we simulate a thrown error in the callback by not returning
                    // a response and instead triggering the error sink.
                    // Since our test fake doesn't inject lastError directly,
                    // we simulate the failure by having the responder not call cb,
                    // then we manually trigger the listener with a failure condition.
                    //
                    // The simplest approach: set lastError before calling cb.
                    const chromeFake = globalThis.__cortexChrome;
                    chromeFake.runtime.lastError = { message: "Simulated failure" };
                    cb?.(undefined);
                    chromeFake.runtime.lastError = undefined;
                    return undefined;
                }
                default:
                    cb?.(undefined);
                    return undefined;
            }
        });

        const { container, cleanup } = await renderPopup();
        try {
            // Bars should still be present (cached data is shown)
            const bars = container.querySelectorAll('[data-testid^="trends-bar-"]');
            expect(bars.length).toBe(7);

            // Stale badge should appear
            const badge = container.querySelector('[data-testid="trends-stale-badge"]');
            expect(badge).not.toBeNull();
            expect((badge?.textContent ?? "").toLowerCase()).toContain("stale");
        } finally {
            await cleanup();
        }
    });

    it("does NOT show the stale badge when load succeeds", async () => {
        installResponder(
            defaultResponder({
                GET_CACHED_TRENDS: {
                    trends: FAKE_TRENDS,
                    timestamp: Date.now() - 5_000, // fresh
                },
            }),
        );

        const { container, cleanup } = await renderPopup();
        try {
            // Bars visible
            const bars = container.querySelectorAll('[data-testid^="trends-bar-"]');
            expect(bars.length).toBe(7);

            // No stale badge
            const badge = container.querySelector('[data-testid="trends-stale-badge"]');
            expect(badge).toBeNull();
        } finally {
            await cleanup();
        }
    });

    it("shows the error copy (not stale badge) when load fails with a lastError and NO cached trends", async () => {
        // Simulate a hard failure (lastError) with no cached trends.
        installResponder((msg, cb) => {
            switch (msg.type) {
                case "GET_STATE":
                    cb?.({ connected: false, state: null, intervention: null, focusSession: null });
                    return undefined;
                case "GET_DAILY_STATS":
                    cb?.(null);
                    return undefined;
                case "GET_CACHED_RECAP":
                    cb?.({ recap: null, timestamp: null });
                    return undefined;
                case "GET_CACHED_TRENDS": {
                    // Inject lastError → triggers setLoadFailed(true); no trends returned.
                    const chromeFake = globalThis.__cortexChrome;
                    chromeFake.runtime.lastError = { message: "Simulated background failure" };
                    cb?.(undefined);
                    chromeFake.runtime.lastError = undefined;
                    return undefined;
                }
                default:
                    cb?.(undefined);
                    return undefined;
            }
        });

        const { container, cleanup } = await renderPopup();
        try {
            // Error copy shown (loadFailed=true, trends=null)
            const errEl = container.querySelector('[data-testid="trends-error"]');
            expect(errEl).not.toBeNull();

            // No stale badge because there are no cached trends to show
            const badge = container.querySelector('[data-testid="trends-stale-badge"]');
            expect(badge).toBeNull();
        } finally {
            await cleanup();
        }
    });
});
