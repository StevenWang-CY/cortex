/**
 * P0 §3.1 / §3.3 — browser-extension popup recap card and
 * "View history" link.
 *
 * The popup must:
 *   - render the recap card when chrome.storage already has a cached
 *     recap inside the 24h TTL,
 *   - dispatch ``RECAP_VIEWED`` on mount so the badge clears,
 *   - dispatch ``OPEN_DASHBOARD_HISTORY`` when the link is clicked and
 *     render the install-hint when the native host is unavailable,
 *   - dispatch ``DISMISS_RECAP`` and drop the card locally when the
 *     user clicks "Dismiss".
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
    sendMessage: ReturnType<typeof vi.fn>;
    cleanup: () => Promise<void>;
}

/**
 * Install a deterministic ``chrome.runtime.sendMessage`` that routes
 * each message type through ``responder`` so individual tests can stub
 * specific responses. The fake from ``test/setup.ts`` returns
 * ``undefined`` for every call which is too coarse for the popup's
 * GET_CACHED_RECAP / GET_STATE / etc. fan-out.
 */
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
    // Let the GET_STATE / GET_DAILY_STATS / GET_CACHED_RECAP callbacks
    // resolve before the test inspects the DOM.
    await act(async () => {
        await new Promise((r) => setTimeout(r, 0));
    });
    const cleanup = async () => {
        await act(async () => {
            root.unmount();
        });
        container.remove();
    };
    return {
        container,
        sendMessage: globalThis.__cortexChrome.runtime.sendMessage as ReturnType<typeof vi.fn>,
        cleanup,
    };
}

const FAKE_RECAP = {
    session_id: "test-session-0",
    start_time: "2026-05-25T10:00:00Z",
    end_time: "2026-05-25T10:38:00Z",
    duration_seconds: 38 * 60,
    flow_percentage: 58,
    breaks_taken: 1,
    longest_flow_streak_seconds: 14 * 60,
    avg_hr_bpm: 72,
};

describe("popup view history link + recap card", () => {
    beforeEach(() => {
        // Make sure each test runs against a fresh storage backing
        // store; the global setup wipes storage too, but tests in this
        // file rely on it being empty unless they explicitly seed it.
        globalThis.__cortexChrome.storage.local.__reset({});
    });

    afterEach(() => {
        vi.useRealTimers();
    });

    it("renders View history link and dispatches OPEN_DASHBOARD_HISTORY on click", async () => {
        installResponder((msg, cb) => {
            if (msg.type === "GET_CACHED_RECAP") {
                cb?.({ recap: null, timestamp: null });
                return undefined;
            }
            if (msg.type === "GET_STATE") {
                cb?.({ connected: false, state: null, intervention: null, focusSession: null });
                return undefined;
            }
            if (msg.type === "GET_DAILY_STATS") {
                cb?.(null);
                return undefined;
            }
            if (msg.type === "OPEN_DASHBOARD_HISTORY") {
                cb?.({ status: "ok" });
                return undefined;
            }
            cb?.(undefined);
            return undefined;
        });

        const { container, sendMessage, cleanup } = await renderPopup();
        try {
            const link = container.querySelector(
                '[data-testid="view-history-link"]',
            ) as HTMLButtonElement | null;
            expect(link).not.toBeNull();
            expect(link?.textContent).toContain("View history");

            await act(async () => {
                link?.click();
                await new Promise((r) => setTimeout(r, 0));
            });

            const opened = sendMessage.mock.calls.find(
                (c) => (c[0] as { type?: string }).type === "OPEN_DASHBOARD_HISTORY",
            );
            expect(opened).toBeTruthy();
        } finally {
            await cleanup();
        }
    });

    it("renders the install hint when OPEN_DASHBOARD_HISTORY reports unavailable", async () => {
        installResponder((msg, cb) => {
            if (msg.type === "GET_CACHED_RECAP") {
                cb?.({ recap: null, timestamp: null });
                return undefined;
            }
            if (msg.type === "GET_STATE") {
                cb?.({ connected: false, state: null, intervention: null, focusSession: null });
                return undefined;
            }
            if (msg.type === "GET_DAILY_STATS") {
                cb?.(null);
                return undefined;
            }
            if (msg.type === "OPEN_DASHBOARD_HISTORY") {
                cb?.({ status: "unavailable" });
                return undefined;
            }
            cb?.(undefined);
            return undefined;
        });

        const { container, cleanup } = await renderPopup();
        try {
            const link = container.querySelector(
                '[data-testid="view-history-link"]',
            ) as HTMLButtonElement | null;
            await act(async () => {
                link?.click();
                await new Promise((r) => setTimeout(r, 0));
            });
            const status = container.querySelector(
                '[data-testid="view-history-status"]',
            );
            expect(status).not.toBeNull();
            expect(status?.textContent ?? "").toMatch(/desktop/i);
        } finally {
            await cleanup();
        }
    });

    it("renders the recap card when chrome.storage has a cached recap", async () => {
        installResponder((msg, cb) => {
            if (msg.type === "GET_CACHED_RECAP") {
                cb?.({ recap: FAKE_RECAP, timestamp: Date.now() - 5_000 });
                return undefined;
            }
            if (msg.type === "GET_STATE") {
                cb?.({ connected: false, state: null, intervention: null, focusSession: null });
                return undefined;
            }
            if (msg.type === "GET_DAILY_STATS") {
                cb?.(null);
                return undefined;
            }
            cb?.(undefined);
            return undefined;
        });

        const { container, cleanup } = await renderPopup();
        try {
            const card = container.querySelector('[data-testid="recap-card"]');
            expect(card).not.toBeNull();
            const text = card?.textContent ?? "";
            // 38m total, 58% flow, 1 break, 14m streak, 72 bpm.
            expect(text).toContain("38m");
            expect(text).toContain("58%");
            expect(text).toContain("1 break");
            expect(text).toContain("14m");
            expect(text).toContain("72 bpm");
            // P0 §3.3 hardening: the schema field is ``avg_hr_bpm``
            // (mean across the session, not peak). Label must match
            // the field semantics — previously "Peak HR" misled.
            expect(text).toContain("Avg HR");
            expect(text).not.toContain("Peak HR");
        } finally {
            await cleanup();
        }
    });

    it("clears the badge via RECAP_VIEWED on mount when a recap is showing", async () => {
        installResponder((msg, cb) => {
            if (msg.type === "GET_CACHED_RECAP") {
                cb?.({ recap: FAKE_RECAP, timestamp: Date.now() });
                return undefined;
            }
            if (msg.type === "GET_STATE") {
                cb?.({ connected: false, state: null, intervention: null, focusSession: null });
                return undefined;
            }
            if (msg.type === "GET_DAILY_STATS") {
                cb?.(null);
                return undefined;
            }
            cb?.(undefined);
            return undefined;
        });

        const { sendMessage, cleanup } = await renderPopup();
        try {
            const recapViewed = sendMessage.mock.calls.find(
                (c) => (c[0] as { type?: string }).type === "RECAP_VIEWED",
            );
            expect(recapViewed).toBeTruthy();
        } finally {
            await cleanup();
        }
    });

    it("does not render the recap card when the cached recap is older than 24h", async () => {
        installResponder((msg, cb) => {
            if (msg.type === "GET_CACHED_RECAP") {
                // 25 hours old → over the TTL.
                cb?.({
                    recap: FAKE_RECAP,
                    timestamp: Date.now() - 25 * 60 * 60 * 1000,
                });
                return undefined;
            }
            if (msg.type === "GET_STATE") {
                cb?.({ connected: false, state: null, intervention: null, focusSession: null });
                return undefined;
            }
            if (msg.type === "GET_DAILY_STATS") {
                cb?.(null);
                return undefined;
            }
            cb?.(undefined);
            return undefined;
        });

        const { container, cleanup } = await renderPopup();
        try {
            expect(
                container.querySelector('[data-testid="recap-card"]'),
            ).toBeNull();
        } finally {
            await cleanup();
        }
    });

    it("dismisses the recap and clears local state when Dismiss is clicked", async () => {
        installResponder((msg, cb) => {
            if (msg.type === "GET_CACHED_RECAP") {
                cb?.({ recap: FAKE_RECAP, timestamp: Date.now() });
                return undefined;
            }
            if (msg.type === "GET_STATE") {
                cb?.({ connected: false, state: null, intervention: null, focusSession: null });
                return undefined;
            }
            if (msg.type === "GET_DAILY_STATS") {
                cb?.(null);
                return undefined;
            }
            cb?.(undefined);
            return undefined;
        });

        const { container, sendMessage, cleanup } = await renderPopup();
        try {
            expect(
                container.querySelector('[data-testid="recap-card"]'),
            ).not.toBeNull();

            const dismissBtn = container.querySelector(
                '[data-testid="recap-dismiss"]',
            ) as HTMLButtonElement | null;
            expect(dismissBtn).not.toBeNull();

            await act(async () => {
                dismissBtn?.click();
                await new Promise((r) => setTimeout(r, 0));
            });

            // Card removed from the DOM
            expect(
                container.querySelector('[data-testid="recap-card"]'),
            ).toBeNull();

            // Background script told to drop the cached recap + badge.
            const dismissCall = sendMessage.mock.calls.find(
                (c) => (c[0] as { type?: string }).type === "DISMISS_RECAP",
            );
            expect(dismissCall).toBeTruthy();
        } finally {
            await cleanup();
        }
    });

    it("re-renders when SESSION_RECAP_READY is broadcast after mount", async () => {
        installResponder((msg, cb) => {
            if (msg.type === "GET_CACHED_RECAP") {
                // No recap initially.
                cb?.({ recap: null, timestamp: null });
                return undefined;
            }
            if (msg.type === "GET_STATE") {
                cb?.({ connected: false, state: null, intervention: null, focusSession: null });
                return undefined;
            }
            if (msg.type === "GET_DAILY_STATS") {
                cb?.(null);
                return undefined;
            }
            cb?.(undefined);
            return undefined;
        });

        const { container, cleanup } = await renderPopup();
        try {
            // No card initially.
            expect(
                container.querySelector('[data-testid="recap-card"]'),
            ).toBeNull();

            // Simulate background broadcasting a fresh recap.
            const fake = globalThis.__cortexChrome;
            const calls = fake.runtime.onMessage.addListener.mock.calls;
            const listener = calls[calls.length - 1][0] as (
                msg: Record<string, unknown>,
            ) => void;
            await act(async () => {
                listener({
                    type: "SESSION_RECAP_READY",
                    payload: FAKE_RECAP,
                    timestamp: Date.now(),
                });
                await new Promise((r) => setTimeout(r, 0));
            });

            expect(
                container.querySelector('[data-testid="recap-card"]'),
            ).not.toBeNull();
        } finally {
            await cleanup();
        }
    });

    it("View history link carries an aria-label and aria-hidden arrow glyph", async () => {
        installResponder((msg, cb) => {
            if (msg.type === "GET_CACHED_RECAP") {
                cb?.({ recap: null, timestamp: null });
                return undefined;
            }
            if (msg.type === "GET_STATE") {
                cb?.({ connected: false, state: null, intervention: null, focusSession: null });
                return undefined;
            }
            if (msg.type === "GET_DAILY_STATS") {
                cb?.(null);
                return undefined;
            }
            cb?.(undefined);
            return undefined;
        });

        const { container, cleanup } = await renderPopup();
        try {
            const link = container.querySelector(
                '[data-testid="view-history-link"]',
            ) as HTMLButtonElement | null;
            expect(link).not.toBeNull();
            // P0 §3.3 hardening: screen readers must hear an action
            // sentence, not just "View history arrow right".
            expect(link?.getAttribute("aria-label")).toBe(
                "Open History tab in desktop dashboard",
            );
            // The "→" glyph itself is presentational — it must be
            // wrapped in aria-hidden so the SR doesn't double-up the
            // affordance with "right arrow" / "right pointing arrow".
            const hiddenSpan = link?.querySelector(
                'span[aria-hidden="true"]',
            );
            expect(hiddenSpan).not.toBeNull();
            expect(hiddenSpan?.textContent ?? "").toContain("→");
        } finally {
            await cleanup();
        }
    });

    it("auto-dismisses the recap card when the 24h TTL crosses", async () => {
        // Pin wall-clock so the TTL math is deterministic.
        const NOW = 1_700_000_000_000;
        vi.useFakeTimers();
        vi.setSystemTime(NOW);

        // Cache the recap with a timestamp 23h old → 1h remaining.
        const RECAP_TS = NOW - 23 * 60 * 60 * 1000;
        installResponder((msg, cb) => {
            if (msg.type === "GET_CACHED_RECAP") {
                cb?.({ recap: FAKE_RECAP, timestamp: RECAP_TS });
                return undefined;
            }
            if (msg.type === "GET_STATE") {
                cb?.({ connected: false, state: null, intervention: null, focusSession: null });
                return undefined;
            }
            if (msg.type === "GET_DAILY_STATS") {
                cb?.(null);
                return undefined;
            }
            cb?.(undefined);
            return undefined;
        });

        const container = document.createElement("div");
        document.body.appendChild(container);
        const root = (await import("react-dom/client")).createRoot(container);
        await act(async () => {
            root.render(React.createElement(CortexPopup));
        });
        await act(async () => {
            await vi.advanceTimersByTimeAsync(0);
        });
        try {
            // Card is visible at t = 23h.
            expect(
                container.querySelector('[data-testid="recap-card"]'),
            ).not.toBeNull();

            // Advance just before TTL crosses — card still up.
            await act(async () => {
                await vi.advanceTimersByTimeAsync(
                    1 * 60 * 60 * 1000 - 1000,
                );
            });
            expect(
                container.querySelector('[data-testid="recap-card"]'),
            ).not.toBeNull();

            // Cross the TTL — auto-dismiss handler fires.
            await act(async () => {
                await vi.advanceTimersByTimeAsync(2_000);
            });
            expect(
                container.querySelector('[data-testid="recap-card"]'),
            ).toBeNull();
        } finally {
            await act(async () => {
                root.unmount();
            });
            container.remove();
            vi.useRealTimers();
        }
    });

    it("clears the auto-dismiss timer on unmount (no leaks)", async () => {
        const NOW = 1_700_000_000_000;
        vi.useFakeTimers();
        vi.setSystemTime(NOW);
        const RECAP_TS = NOW - 23 * 60 * 60 * 1000;
        installResponder((msg, cb) => {
            if (msg.type === "GET_CACHED_RECAP") {
                cb?.({ recap: FAKE_RECAP, timestamp: RECAP_TS });
                return undefined;
            }
            if (msg.type === "GET_STATE") {
                cb?.({ connected: false, state: null, intervention: null, focusSession: null });
                return undefined;
            }
            if (msg.type === "GET_DAILY_STATS") {
                cb?.(null);
                return undefined;
            }
            cb?.(undefined);
            return undefined;
        });

        const container = document.createElement("div");
        document.body.appendChild(container);
        const root = (await import("react-dom/client")).createRoot(container);
        await act(async () => {
            root.render(React.createElement(CortexPopup));
        });
        await act(async () => {
            await vi.advanceTimersByTimeAsync(0);
        });

        // Before-unmount: card is up and a setTimeout is armed.
        expect(
            container.querySelector('[data-testid="recap-card"]'),
        ).not.toBeNull();
        const timersBefore = vi.getTimerCount();
        expect(timersBefore).toBeGreaterThanOrEqual(1);

        // Unmount and confirm the TTL timer was cleared.
        await act(async () => {
            root.unmount();
        });
        container.remove();
        // The Recap TTL timer must be cleared on unmount; React's own
        // effect-flush may still hold one transient timer so we only
        // require that we cleared at least one timer.
        expect(vi.getTimerCount()).toBeLessThan(timersBefore);

        vi.useRealTimers();
    });
});
