/**
 * F15: WS streaming JSON parse failures surfaced.
 *
 * - Single malformed frame: counter incremented, no reconnect.
 * - Three malformed frames within 10s: counter reset, ws.close called.
 * - A clean frame resets the counter.
 *
 * On `main` the bare `catch { return; }` swallows the error: no log,
 * no metric, no reconnect.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { getLatestSocket } from "../test/mocks/websocket";

async function setup() {
    vi.resetModules();
    const mod = await import("../background");
    await new Promise((r) => setTimeout(r, 0));
    const sock = getLatestSocket();
    if (!sock) throw new Error("WebSocket fake missing");
    return {
        sock,
        mod: mod as unknown as {
            _resetWsParseErrorCounter: () => void;
            _getWsParseErrorCount: () => number;
        },
    };
}

describe("F15 WS parse-error surfacing", () => {
    beforeEach(() => {
        vi.resetModules();
        vi.useRealTimers();
    });

    it("counts a single bad frame without reconnecting", async () => {
        const { sock, mod } = await setup();
        const warn = vi.spyOn(console, "warn").mockImplementation(() => {});

        sock.__deliver("{not json");

        expect(mod._getWsParseErrorCount()).toBe(1);
        expect(sock.closedCalls.length).toBe(0);
        expect(warn).toHaveBeenCalled();
        warn.mockRestore();
    });

    it("forces a reconnect on three bad frames in the window", async () => {
        const { sock } = await setup();
        const warn = vi.spyOn(console, "warn").mockImplementation(() => {});

        sock.__deliver("{nope1");
        sock.__deliver("{nope2");
        sock.__deliver("{nope3");

        expect(sock.closedCalls.length).toBeGreaterThanOrEqual(1);
        expect(sock.closedCalls[0].reason).toBe("cortex.ws.parse_error_storm");
        warn.mockRestore();
    });

    it("resets the counter on a successful parse", async () => {
        const { sock, mod } = await setup();
        const warn = vi.spyOn(console, "warn").mockImplementation(() => {});

        sock.__deliver("{still bad");
        expect(mod._getWsParseErrorCount()).toBe(1);

        sock.__deliver({
            type: "STATE_UPDATE",
            payload: {
                state: "FLOW",
                confidence: 0.5,
                scores: {},
                signal_quality: {},
                dwell_seconds: 0,
                reasons: [],
            },
            timestamp: Date.now() / 1000,
            sequence: 1,
        });

        expect(mod._getWsParseErrorCount()).toBe(0);
        warn.mockRestore();
    });
});
