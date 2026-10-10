import { describe, expect, it, vi } from "vitest";
import { getLatestSocket } from "../test/mocks/websocket";

describe("worker state hydration", () => {
    it("returns the same evidence flags as live updates with the sample's original age", async () => {
        vi.resetModules();
        let clock = 100;
        const monotonic = vi.spyOn(performance, "now").mockImplementation(() => clock);
        try {
            await import("../background");
            await new Promise(resolve => setTimeout(resolve, 0));
            const socket = getLatestSocket()!;
            socket.__open();
            socket.__deliver({ type: "STATE_UPDATE", sequence: 1, payload: {
                state: "UNKNOWN", status: "warming_up", confidence: 0, scores: {}, signal_quality: {}, dwell_seconds: 0, reasons: [],
                biometrics: { heart_rate: 72, blink_rate: 10 },
                capture: { stale: true, frames_flowing: false, face_detected: true,
                    pulse_unavailable: { code: "no_observations", message: "waiting" } },
                store: { degraded: true },
            } });
            await new Promise(resolve => setTimeout(resolve, 0));
            clock = 4100;
            const hydrated = await new Promise<Record<string, unknown>>(resolve => {
                globalThis.__cortexChrome.runtime.onMessage.__dispatch({ type: "GET_STATE" }, {}, resolve);
            });
            expect(hydrated.stateAgeMs).toBe(4000);
            expect(hydrated.state).toMatchObject({ biometrics: { heart_rate: 72 },
                capture: { stale: true, frames_flowing: false, pulse_unavailable: { code: "no_observations" } }, store: { degraded: true } });
            const live = globalThis.__cortexChrome.runtime.sendMessage.mock.calls
                .map(call => call[0]).find(msg => msg.type === "STATE_UPDATE");
            expect(live.payload).toEqual(hydrated.state);
            socket.__remoteClose(1006, "network dropped");
            const offline = await new Promise<Record<string, unknown>>(resolve => {
                globalThis.__cortexChrome.runtime.onMessage.__dispatch({ type: "GET_STATE" }, {}, resolve);
            });
            expect(offline.state).toBeNull();
            expect(offline.stateAgeMs).toBeNull();
        } finally { monotonic.mockRestore(); }
    });
});
