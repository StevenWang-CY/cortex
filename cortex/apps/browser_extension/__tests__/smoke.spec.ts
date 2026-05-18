/**
 * F40 smoke test: importing background.ts under the chrome/WebSocket
 * fakes must not throw, and delivering a STATE_UPDATE through the mock
 * WebSocket must not crash either.
 *
 * This test fails on `main` (no vitest config + missing fakes) and
 * passes after F40 lands the test infra.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { getLatestSocket } from "../test/mocks/websocket";

describe("background.ts smoke", () => {
    beforeEach(() => {
        vi.resetModules();
    });

    it("imports without throwing", async () => {
        await expect(import("../background")).resolves.toBeTruthy();
    });

    it("accepts a STATE_UPDATE through the mock WebSocket without exception", async () => {
        await import("../background");
        // Wait for the auto-open microtask to fire.
        await new Promise((r) => setTimeout(r, 0));
        const sock = getLatestSocket();
        expect(sock).not.toBeNull();
        const frame = {
            type: "STATE_UPDATE",
            payload: {
                state: "FLOW",
                confidence: 0.8,
                scores: {},
                signal_quality: {},
                dwell_seconds: 10,
                reasons: [],
            },
            timestamp: Date.now() / 1000,
            sequence: 1,
        };
        expect(() => sock!.__deliver(frame)).not.toThrow();
    });
});
