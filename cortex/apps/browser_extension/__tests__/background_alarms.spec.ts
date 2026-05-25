/**
 * P0 §3.2 — chrome.alarms drives the weekly-trends refresh.
 *
 * MV3 service workers are evicted after ~30s of idle and any in-memory
 * ``setInterval`` handle dies with them. The trends-refresh schedule
 * therefore lives on ``chrome.alarms`` (the only persistence-safe
 * scheduler available in MV3).
 *
 * Contract:
 *   1. The cold-start path registers ``cortex-trends-refresh`` with a
 *      30-minute period at module load time.
 *   2. ``chrome.runtime.onInstalled`` re-registers it (so an extension
 *      update doesn't leave the schedule in an indeterminate state).
 *   3. ``chrome.runtime.onStartup`` re-registers it (browser-restart
 *      activation in MV3 doesn't always carry alarms across).
 *   4. When the alarm fires AND we are currently WS-connected, the
 *      background script sends a REQUEST_TRENDS frame to the daemon.
 *   5. When the alarm fires AND we are disconnected, the frame is
 *      suppressed (a stale send would error on a closed socket).
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { getLatestSocket } from "../test/mocks/websocket";

const TRENDS_ALARM = "cortex-trends-refresh";

async function setup() {
    vi.resetModules();
    process.env.CORTEX_DEBUG = "true";
    await import("../background");
    await new Promise((r) => setTimeout(r, 0));
    return { fake: globalThis.__cortexChrome };
}

describe("P0 §3.2 — chrome.alarms trends-refresh schedule", () => {
    beforeEach(() => {
        vi.resetModules();
    });

    it("registers cortex-trends-refresh with a 30-minute period at module load", async () => {
        const { fake } = await setup();
        const createCalls = (
            fake.alarms.create as ReturnType<typeof vi.fn>
        ).mock.calls;
        const trendsAlarm = createCalls.find(
            (c) => (c[0] as string) === TRENDS_ALARM,
        );
        expect(trendsAlarm).toBeDefined();
        const opts = trendsAlarm![1] as { periodInMinutes?: number };
        expect(opts.periodInMinutes).toBe(30);
    });

    it("re-registers cortex-trends-refresh from chrome.runtime.onInstalled", async () => {
        const { fake } = await setup();
        const createSpy = fake.alarms.create as ReturnType<typeof vi.fn>;
        const callsBefore = createSpy.mock.calls.length;

        // Fire the onInstalled event the way Chrome would on an
        // extension update.
        fake.runtime.onInstalled.__dispatch({ reason: "update" });
        await new Promise((r) => setTimeout(r, 0));

        const newCalls = createSpy.mock.calls
            .slice(callsBefore)
            .filter((c) => (c[0] as string) === TRENDS_ALARM);
        expect(newCalls.length).toBeGreaterThanOrEqual(1);
        const opts = newCalls[0][1] as { periodInMinutes?: number };
        expect(opts.periodInMinutes).toBe(30);
    });

    it("re-registers cortex-trends-refresh from chrome.runtime.onStartup", async () => {
        const { fake } = await setup();
        const createSpy = fake.alarms.create as ReturnType<typeof vi.fn>;
        const callsBefore = createSpy.mock.calls.length;

        fake.runtime.onStartup.__dispatch();
        await new Promise((r) => setTimeout(r, 0));

        const newCalls = createSpy.mock.calls
            .slice(callsBefore)
            .filter((c) => (c[0] as string) === TRENDS_ALARM);
        expect(newCalls.length).toBeGreaterThanOrEqual(1);
        const opts = newCalls[0][1] as { periodInMinutes?: number };
        expect(opts.periodInMinutes).toBe(30);
    });

    it("sends REQUEST_TRENDS to the daemon when the alarm fires AND connected", async () => {
        const { fake } = await setup();
        // The fake WS auto-opens on the next microtask; wait so the
        // connected flag flips before we fire the alarm.
        for (let i = 0; i < 5; i++) {
            await new Promise((r) => setTimeout(r, 0));
        }
        const sock = getLatestSocket();
        if (!sock) throw new Error("WebSocket fake missing");
        // Background's connect handler also fires AUTH then sets
        // ``connected=true`` only after the daemon responds AUTH_OK.
        // Deliver the ack so the trends alarm has a connected path
        // to send through.
        sock.__deliver({
            type: "AUTH_OK",
            payload: { ok: true },
            timestamp: Date.now() / 1000,
            sequence: 0,
        });
        for (let i = 0; i < 5; i++) {
            await new Promise((r) => setTimeout(r, 0));
        }
        const sentBefore = sock.sent.length;

        // Fire the alarm.
        fake.alarms.onAlarm.__dispatch({
            name: TRENDS_ALARM,
            scheduledTime: Date.now(),
        });
        for (let i = 0; i < 5; i++) {
            await new Promise((r) => setTimeout(r, 0));
        }

        const newSends = sock.sent
            .slice(sentBefore)
            .map((raw) => JSON.parse(raw) as { type?: string; payload?: Record<string, unknown> });
        const trendsSend = newSends.find((m) => m.type === "REQUEST_TRENDS");
        expect(trendsSend).toBeDefined();
        expect(trendsSend!.payload?.window).toBe("week");
        expect(trendsSend!.payload?.refresh).toBe(false);
    });

    it("does NOT send REQUEST_TRENDS when the alarm fires while disconnected", async () => {
        const { fake } = await setup();
        const sock = getLatestSocket();
        if (!sock) throw new Error("WebSocket fake missing");
        // Force the socket closed so ``connected`` is false at alarm time.
        sock.__remoteClose(1006, "test-disconnected");
        for (let i = 0; i < 5; i++) {
            await new Promise((r) => setTimeout(r, 0));
        }
        const sentBefore = sock.sent.length;

        fake.alarms.onAlarm.__dispatch({
            name: TRENDS_ALARM,
            scheduledTime: Date.now(),
        });
        for (let i = 0; i < 5; i++) {
            await new Promise((r) => setTimeout(r, 0));
        }

        const newSends = sock.sent
            .slice(sentBefore)
            .map((raw) => JSON.parse(raw) as { type?: string });
        const trendsSend = newSends.find((m) => m.type === "REQUEST_TRENDS");
        expect(trendsSend).toBeUndefined();
    });

    it("ignores onAlarm events for other alarm names (no extra trends sends)", async () => {
        const { fake } = await setup();
        for (let i = 0; i < 5; i++) {
            await new Promise((r) => setTimeout(r, 0));
        }
        const sock = getLatestSocket();
        if (!sock) throw new Error("WebSocket fake missing");
        sock.__deliver({
            type: "AUTH_OK",
            payload: { ok: true },
            timestamp: Date.now() / 1000,
            sequence: 0,
        });
        for (let i = 0; i < 5; i++) {
            await new Promise((r) => setTimeout(r, 0));
        }
        const sentBefore = sock.sent.length;

        // Wrong alarm name; the trends handler should be inert.
        fake.alarms.onAlarm.__dispatch({
            name: "cortex-some-other-alarm",
            scheduledTime: Date.now(),
        });
        for (let i = 0; i < 5; i++) {
            await new Promise((r) => setTimeout(r, 0));
        }

        const newSends = sock.sent
            .slice(sentBefore)
            .map((raw) => JSON.parse(raw) as { type?: string });
        const trendsSend = newSends.find((m) => m.type === "REQUEST_TRENDS");
        expect(trendsSend).toBeUndefined();
    });
});
