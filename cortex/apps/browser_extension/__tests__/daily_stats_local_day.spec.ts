/**
 * The popup's Today footer counts the user's local day, and only today.
 *
 * ``saveToDailyStats`` keyed the day with ``toISOString().slice(0, 10)``,
 * the UTC date, so the footer rolled over at 19:00 in New York (20:00 in
 * winter) and 08:00 in Beijing: an evening session was filed under
 * tomorrow, and the morning's work under yesterday. ``GET_DAILY_STATS``
 * also returned whatever was stored, so the footer showed an earlier
 * day's totals as today's until the next session ended.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getLatestSocket } from "../test/mocks/websocket";

type BgListener = (
    msg: Record<string, unknown>,
    sender: unknown,
    sendResponse: (resp: unknown) => void,
) => unknown;

const ORIGINAL_TZ = process.env.TZ;

async function boot(): Promise<BgListener> {
    vi.resetModules();
    await import("../background");
    await Promise.resolve();
    expect(getLatestSocket()).toBeTruthy();
    const fake = globalThis.__cortexChrome;
    return fake.runtime.onMessage.addListener.mock.calls[0][0] as BgListener;
}

function ask(listener: BgListener, msg: Record<string, unknown>): Promise<unknown> {
    return new Promise((resolve) => {
        listener(msg, undefined, resolve);
    });
}

describe("daily stats follow the local calendar day", () => {
    beforeEach(() => {
        process.env.TZ = "America/New_York";
        vi.useFakeTimers({ toFake: ["Date"] });
    });

    afterEach(() => {
        vi.useRealTimers();
        process.env.TZ = ORIGINAL_TZ;
    });

    it("a session ending at 23:30 local is filed under that local day", async () => {
        // 2026-11-26T04:30Z is 23:30 on 2026-11-25 in New York.
        vi.setSystemTime(new Date("2026-11-26T04:30:00Z"));
        const listener = await boot();
        listener({ type: "START_FOCUS", goal: "write the report" }, undefined, () => {});
        listener({ type: "STOP_FOCUS" }, undefined, () => {});
        await vi.waitFor(() => {
            const stored = globalThis.__cortexChrome.storage.local.__peek()
                .cortex_daily_stats as { date: string } | undefined;
            expect(stored?.date).toBe("2026-11-25");
        });
    });

    it("an earlier day's totals are not reported as today's", async () => {
        vi.setSystemTime(new Date("2026-11-25T15:00:00Z"));
        globalThis.__cortexChrome.storage.local.__reset({
            cortex_daily_stats: {
                date: "2026-11-24",
                totalFocusMin: 95,
                totalSessionMin: 120,
                sessions: 3,
                distractionsBlocked: 7,
                longestStreakMin: 40,
            },
        });
        const listener = await boot();
        expect(await ask(listener, { type: "GET_DAILY_STATS" })).toBeNull();
    });

    it("today's totals are reported", async () => {
        vi.setSystemTime(new Date("2026-11-25T15:00:00Z"));
        const today = {
            date: "2026-11-25",
            totalFocusMin: 30,
            totalSessionMin: 45,
            sessions: 1,
            distractionsBlocked: 2,
            longestStreakMin: 20,
        };
        globalThis.__cortexChrome.storage.local.__reset({ cortex_daily_stats: today });
        const listener = await boot();
        expect(await ask(listener, { type: "GET_DAILY_STATS" })).toEqual(today);
    });
});
