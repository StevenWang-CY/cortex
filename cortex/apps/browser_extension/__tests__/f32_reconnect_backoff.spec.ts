/**
 * F32: WS reconnect backoff resets to INITIAL on every successful open.
 *
 * Simulates a backoff that drifted up to MAX_RECONNECT_DELAY, then a
 * successful connect, and asserts the next disconnect schedules with
 * the initial delay instead of staying at MAX.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { getLatestSocket } from "../test/mocks/websocket";

async function setup() {
    vi.resetModules();
    const mod = await import("../background");
    await new Promise((r) => setTimeout(r, 0));
    return mod as unknown as {
        _getReconnectDelay: () => number;
        _getInitialReconnectDelay: () => number;
    };
}

describe("F32 reconnect backoff reset", () => {
    beforeEach(() => {
        vi.resetModules();
    });

    it("uses INITIAL_RECONNECT_DELAY on a fresh import", async () => {
        const mod = await setup();
        expect(mod._getReconnectDelay()).toBe(mod._getInitialReconnectDelay());
    });

    it("doubles the delay on disconnect and resets it on the next open", async () => {
        const mod = await setup();
        const sock = getLatestSocket();
        expect(sock).not.toBeNull();

        // After the initial auto-open, delay is at INITIAL.
        expect(mod._getReconnectDelay()).toBe(mod._getInitialReconnectDelay());

        // Drop the socket; scheduleReconnect doubles the delay (e.g. to 6000).
        sock!.__remoteClose(1006, "test_drop");
        const inflatedDelay = mod._getReconnectDelay();
        expect(inflatedDelay).toBeGreaterThan(mod._getInitialReconnectDelay());

        // Wait long enough for the reconnect setTimeout to fire and for
        // the new fake socket's auto-open microtask to run. The reconnect
        // delay is in milliseconds — wait it out plus a small margin.
        await new Promise((r) => setTimeout(r, inflatedDelay + 100));

        // After the new socket auto-opens, F32 must have reset the delay.
        expect(mod._getReconnectDelay()).toBe(mod._getInitialReconnectDelay());
    }, 30_000);
});
