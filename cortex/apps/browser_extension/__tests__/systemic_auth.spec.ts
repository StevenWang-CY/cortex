/**
 * Debt-2 (audit) — Commit 4: browser-extension AUTH-first wiring.
 *
 * With the systemic AUTH-first gate landed in the daemon (Commit 2),
 * every WebSocket client must send an ``AUTH`` frame as the first
 * message after the socket opens. The background service worker now
 * does this automatically: ``getAuthToken()`` resolves a cached or
 * native-host-fetched token, the background ``onopen`` handler sends
 * ``AUTH`` first and only then ``IDENTIFY``.
 *
 * This spec proves:
 *
 * 1. The first frame the background ever sends on a fresh socket is
 *    ``AUTH`` with the cached capability token in ``payload.auth_token``.
 * 2. ``IDENTIFY`` follows AUTH (not before it). Without this ordering
 *    the daemon would close the socket on the IDENTIFY frame.
 * 3. After AUTH succeeds (we simulate the daemon's ``AUTH_OK`` reply
 *    and a follow-on ``STATE_UPDATE``), inbound state messages reach
 *    the popup-broadcast path unblocked.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { getLatestSocket } from "../test/mocks/websocket";

interface WSFrame {
    type: string;
    payload: Record<string, unknown>;
    timestamp: number;
    sequence: number;
    correlation_id?: string;
}

describe("Debt-2 systemic auth — WS handshake order", () => {
    beforeEach(() => {
        vi.resetModules();
    });

    it("first outbound frame is AUTH with the cached token, IDENTIFY follows", async () => {
        const fake = globalThis.__cortexChrome;
        // Native host returns a stable token for ``get_auth_token``.
        fake.runtime.sendNativeMessage.mockImplementation(
            (
                _app: string,
                msg: unknown,
                cb?: (resp: unknown) => void,
            ) => {
                const m = msg as { command?: string };
                if (m?.command === "get_auth_token") {
                    if (cb) cb({ auth_token: "tok_debt2_xyz" });
                    return Promise.resolve({ auth_token: "tok_debt2_xyz" });
                }
                if (cb) cb({ status: "ok" });
                return Promise.resolve({ status: "ok" });
            },
        );

        await import("../background");
        // Allow microtasks: socket opens, getAuthToken roundtrips,
        // background sends AUTH and IDENTIFY.
        for (let i = 0; i < 8; i++) {
            await new Promise((r) => setTimeout(r, 0));
        }

        const sock = getLatestSocket();
        expect(sock).not.toBeNull();
        expect(sock!.sent.length).toBeGreaterThanOrEqual(2);

        const frames = sock!.sent.map((s) => JSON.parse(s) as WSFrame);

        // First frame: AUTH with the cached token.
        expect(frames[0].type).toBe("AUTH");
        expect(frames[0].payload.auth_token).toBe("tok_debt2_xyz");

        // IDENTIFY MUST come after AUTH so the daemon's gate accepts it.
        const authIdx = frames.findIndex((f) => f.type === "AUTH");
        const identifyIdx = frames.findIndex((f) => f.type === "IDENTIFY");
        expect(authIdx).toBe(0);
        expect(identifyIdx).toBeGreaterThan(authIdx);
        expect(frames[identifyIdx].payload.client_type).toBe("chrome");
    });

    it("STATE_UPDATE arrives unblocked after the AUTH handshake", async () => {
        const fake = globalThis.__cortexChrome;
        fake.runtime.sendNativeMessage.mockImplementation(
            (
                _app: string,
                msg: unknown,
                cb?: (resp: unknown) => void,
            ) => {
                const m = msg as { command?: string };
                if (m?.command === "get_auth_token") {
                    if (cb) cb({ auth_token: "tok_state_test" });
                    return Promise.resolve({ auth_token: "tok_state_test" });
                }
                if (cb) cb({ status: "ok" });
                return Promise.resolve({ status: "ok" });
            },
        );

        await import("../background");
        for (let i = 0; i < 8; i++) {
            await new Promise((r) => setTimeout(r, 0));
        }
        const sock = getLatestSocket();
        expect(sock).not.toBeNull();

        // Simulate the daemon's AUTH_OK then a follow-on STATE_UPDATE.
        sock!.__deliver({
            type: "AUTH_OK",
            payload: {},
            timestamp: Date.now() / 1000,
            sequence: 1,
        });
        sock!.__deliver({
            type: "STATE_UPDATE",
            payload: {
                state: "FLOW",
                confidence: 0.9,
                scores: { flow: 0.9, hypo: 0, hyper: 0.1, recovery: 0 },
                signal_quality: { physio: 1, kinematics: 1, telemetry: 1, overall: 1 },
                dwell_seconds: 10,
                reasons: [],
                stress_integral: 0,
                calibrated_probabilities: null,
                classifier_source: "test",
                classifier_alpha: 1,
                timestamp: Date.now() / 1000,
            },
            timestamp: Date.now() / 1000,
            sequence: 2,
        });

        // The state update should have been broadcast to the popup via
        // ``chrome.runtime.sendMessage``. Confirm the call was made
        // with our payload at least once.
        const calls = fake.runtime.sendMessage.mock.calls;
        const stateBroadcast = calls.find((args: unknown[]) => {
            const m = args[0] as { type?: string } | undefined;
            return m?.type === "STATE_UPDATE";
        });
        expect(stateBroadcast).toBeDefined();
    });
});
