/**
 * P0 §3.2 — browser-extension popup "Last 7 days" sparkbar strip.
 *
 * The popup must:
 *   - render exactly one bar per ``DailyBaseline`` row (capped at 7),
 *   - render an empty-state copy line when no rows exist (or every
 *     row reports zero focus minutes),
 *   - tint top-quartile bars with the terracotta accent so the best
 *     day reads at a glance,
 *   - fire ``GET_CACHED_TRENDS`` and ``REQUEST_TRENDS`` on mount so
 *     the bars hydrate from cache and nudge a fresh fetch,
 *   - re-render live when the background script broadcasts
 *     ``TRENDS_READY`` after the popup has already mounted,
 *   - surface the average minutes/day in the header.
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
 * specific responses. Mirrors the helper used in
 * ``popup_open_history.spec.tsx``.
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
    // Let the GET_STATE / GET_DAILY_STATS / GET_CACHED_RECAP /
    // GET_CACHED_TRENDS / REQUEST_TRENDS callbacks resolve before
    // the test inspects the DOM.
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

interface DayRow {
    record_date: string;
    total_flow_minutes: number;
}

function makeDaily(rows: Array<[string, number]>): DayRow[] {
    return rows.map(([record_date, total_flow_minutes]) => ({
        record_date,
        total_flow_minutes,
    }));
}

/** Seven days of varied focus minutes; the 75th-percentile cutoff
 *  here is ``28`` minutes so the two top days (45 and 30) come back
 *  as terracotta and the other five fall back to the tertiary grey. */
const SEVEN_DAYS = makeDaily([
    ["2026-05-19", 10],
    ["2026-05-20", 22],
    ["2026-05-21", 15],
    ["2026-05-22", 8],
    ["2026-05-23", 30],
    ["2026-05-24", 45],
    ["2026-05-25", 18],
]);

const FAKE_TRENDS = {
    window: "week" as const,
    daily: SEVEN_DAYS,
    last_aggregated: "2026-05-25T08:00:00Z",
};

/**
 * Default stub: every message type the popup fans out on mount is
 * answered with a benign empty payload. Individual tests override the
 * trends responses via ``overrides``.
 */
function defaultResponder(
    overrides: Partial<Record<string, unknown>> = {},
): SendMessageResponder {
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

describe("popup last-7-days sparkbar strip", () => {
    beforeEach(() => {
        globalThis.__cortexChrome.storage.local.__reset({});
    });

    afterEach(() => {
        vi.useRealTimers();
    });

    it("renders 7 bars when 7 days of cached trends are available", async () => {
        installResponder(
            defaultResponder({
                GET_CACHED_TRENDS: {
                    trends: FAKE_TRENDS,
                    timestamp: Date.now() - 5_000,
                },
            }),
        );

        const { container, cleanup } = await renderPopup();
        try {
            const strip = container.querySelector('[data-testid="trends-strip"]');
            expect(strip).not.toBeNull();
            const bars = container.querySelectorAll('[data-testid^="trends-bar-"]');
            expect(bars.length).toBe(7);
        } finally {
            await cleanup();
        }
    });

    it("renders the empty-state copy when daily is empty", async () => {
        installResponder(
            defaultResponder({
                GET_CACHED_TRENDS: { trends: null, timestamp: null },
                REQUEST_TRENDS: { trends: null, timestamp: null },
            }),
        );

        const { container, cleanup } = await renderPopup();
        try {
            const empty = container.querySelector('[data-testid="trends-empty"]');
            expect(empty).not.toBeNull();
            expect(empty?.textContent ?? "").toMatch(/Not enough data/i);
        } finally {
            await cleanup();
        }
    });

    it("renders the empty-state copy when every day reports zero focus minutes", async () => {
        installResponder(
            defaultResponder({
                GET_CACHED_TRENDS: {
                    trends: {
                        window: "week",
                        daily: makeDaily([
                            ["2026-05-19", 0],
                            ["2026-05-20", 0],
                            ["2026-05-21", 0],
                            ["2026-05-22", 0],
                            ["2026-05-23", 0],
                            ["2026-05-24", 0],
                            ["2026-05-25", 0],
                        ]),
                    },
                    timestamp: Date.now(),
                },
            }),
        );

        const { container, cleanup } = await renderPopup();
        try {
            expect(
                container.querySelector('[data-testid="trends-empty"]'),
            ).not.toBeNull();
            expect(
                container.querySelectorAll('[data-testid^="trends-bar-"]').length,
            ).toBe(0);
        } finally {
            await cleanup();
        }
    });

    it("tints top-quartile bars with the terracotta accent and dims the rest", async () => {
        installResponder(
            defaultResponder({
                GET_CACHED_TRENDS: {
                    trends: FAKE_TRENDS,
                    timestamp: Date.now(),
                },
            }),
        );

        const { container, cleanup } = await renderPopup();
        try {
            const bars = Array.from(
                container.querySelectorAll('[data-testid^="trends-bar-"]'),
            ) as HTMLDivElement[];
            expect(bars.length).toBe(7);

            // ``SEVEN_DAYS`` order: 10, 22, 15, 8, 30, 45, 18
            // sorted asc:           8, 10, 15, 18, 22, 30, 45
            // 75th percentile idx = floor(7 * 0.75) = 5  -> threshold = 30
            // Bars strictly > 30 are hot: just index 5 (45).
            // The implementation's ``data-hot`` attribute carries the
            // classification so we can assert without coupling to the
            // exact CSS colour token at the rendered-style layer.
            const hotIndices = bars
                .map((b, i) => (b.getAttribute("data-hot") === "true" ? i : -1))
                .filter((i) => i >= 0);
            expect(hotIndices).toEqual([5]);

            // The hot bar's inline background should be the terracotta
            // accent token (``#D97757`` in design-tokens.ts). jsdom
            // canonicalises hex to the rgb() form, so accept either.
            const hotBg = (bars[5].style.background || "").toLowerCase();
            expect(
                hotBg.includes("#d97757") ||
                    hotBg.replace(/\s+/g, "") === "rgb(217,119,87)",
            ).toBe(true);

            // A non-hot bar (index 0 = 10 min) should NOT carry the
            // accent — verify it falls back to the tertiary grey.
            const coldBg = (bars[0].style.background || "").toLowerCase();
            expect(coldBg).not.toContain("#d97757");
            expect(coldBg.replace(/\s+/g, "")).not.toBe("rgb(217,119,87)");
        } finally {
            await cleanup();
        }
    });

    it("treats every bar as hot when fewer than 4 non-zero days exist", async () => {
        installResponder(
            defaultResponder({
                GET_CACHED_TRENDS: {
                    trends: {
                        window: "week",
                        daily: makeDaily([
                            ["2026-05-24", 12],
                            ["2026-05-25", 22],
                        ]),
                    },
                    timestamp: Date.now(),
                },
            }),
        );

        const { container, cleanup } = await renderPopup();
        try {
            const bars = Array.from(
                container.querySelectorAll('[data-testid^="trends-bar-"]'),
            ) as HTMLDivElement[];
            expect(bars.length).toBe(2);
            for (const b of bars) {
                expect(b.getAttribute("data-hot")).toBe("true");
            }
        } finally {
            await cleanup();
        }
    });

    it("fires GET_CACHED_TRENDS and REQUEST_TRENDS on mount", async () => {
        installResponder(
            defaultResponder({
                GET_CACHED_TRENDS: { trends: null, timestamp: null },
                REQUEST_TRENDS: { trends: null, timestamp: null },
            }),
        );

        const { sendMessage, cleanup } = await renderPopup();
        try {
            const getCached = sendMessage.mock.calls.find(
                (c) => (c[0] as { type?: string }).type === "GET_CACHED_TRENDS",
            );
            expect(getCached).toBeTruthy();
            const requestTrends = sendMessage.mock.calls.find(
                (c) => (c[0] as { type?: string }).type === "REQUEST_TRENDS",
            );
            expect(requestTrends).toBeTruthy();
        } finally {
            await cleanup();
        }
    });

    it("does not fire REQUEST_TRENDS when the cached payload is fresh", async () => {
        installResponder(
            defaultResponder({
                GET_CACHED_TRENDS: {
                    trends: FAKE_TRENDS,
                    timestamp: Date.now() - 60_000, // 1 min old, well inside 6h
                },
            }),
        );

        const { sendMessage, cleanup } = await renderPopup();
        try {
            const requestTrends = sendMessage.mock.calls.find(
                (c) => (c[0] as { type?: string }).type === "REQUEST_TRENDS",
            );
            // The fresh-cache branch deliberately skips the nudge to
            // avoid hammering the daemon's aggregator every popup open.
            expect(requestTrends).toBeFalsy();
        } finally {
            await cleanup();
        }
    });

    it("updates the bars live when TRENDS_READY is broadcast after mount", async () => {
        installResponder(
            defaultResponder({
                GET_CACHED_TRENDS: { trends: null, timestamp: null },
                REQUEST_TRENDS: { trends: null, timestamp: null },
            }),
        );

        const { container, cleanup } = await renderPopup();
        try {
            // Empty state on first paint.
            expect(
                container.querySelector('[data-testid="trends-empty"]'),
            ).not.toBeNull();

            // Find the strip's own listener: the popup registers its
            // top-level listener last, so the strip's listener sits
            // immediately before it. To stay robust against listener
            // ordering changes we just dispatch into *every* listener
            // — they all filter on msg.type so non-targets are no-ops.
            const fake = globalThis.__cortexChrome;
            const calls = fake.runtime.onMessage.addListener.mock.calls;
            await act(async () => {
                for (const [listener] of calls) {
                    (listener as (m: Record<string, unknown>) => void)({
                        type: "TRENDS_READY",
                        payload: FAKE_TRENDS,
                        timestamp: Date.now(),
                    });
                }
                await new Promise((r) => setTimeout(r, 0));
            });

            const bars = container.querySelectorAll('[data-testid^="trends-bar-"]');
            expect(bars.length).toBe(7);
            // Empty-state copy must be gone now.
            expect(
                container.querySelector('[data-testid="trends-empty"]'),
            ).toBeNull();
        } finally {
            await cleanup();
        }
    });

    it("renders the average minutes/day in the header", async () => {
        installResponder(
            defaultResponder({
                GET_CACHED_TRENDS: {
                    trends: FAKE_TRENDS,
                    timestamp: Date.now(),
                },
            }),
        );

        const { container, cleanup } = await renderPopup();
        try {
            // SEVEN_DAYS sum = 148, /7 = 21.14… → 21 min avg/day.
            const avg = container.querySelector('[data-testid="trends-avg"]');
            expect(avg).not.toBeNull();
            expect(avg?.textContent ?? "").toContain("21 min");
        } finally {
            await cleanup();
        }
    });

    it("caps the strip at 7 bars when the daemon returns more days", async () => {
        const tenDays = makeDaily([
            ["2026-05-16", 5],
            ["2026-05-17", 9],
            ["2026-05-18", 11],
            ["2026-05-19", 10],
            ["2026-05-20", 22],
            ["2026-05-21", 15],
            ["2026-05-22", 8],
            ["2026-05-23", 30],
            ["2026-05-24", 45],
            ["2026-05-25", 18],
        ]);
        installResponder(
            defaultResponder({
                GET_CACHED_TRENDS: {
                    trends: { window: "month", daily: tenDays },
                    timestamp: Date.now(),
                },
            }),
        );

        const { container, cleanup } = await renderPopup();
        try {
            const bars = container.querySelectorAll('[data-testid^="trends-bar-"]');
            expect(bars.length).toBe(7);
        } finally {
            await cleanup();
        }
    });
});
