/**
 * F16: active-intervention atomic swap by correlation_id.
 *
 * Three INTERVENTION_TRIGGER frames arrive in rapid succession. Each
 * carries a distinct `correlation_id`. The popup later sends a single
 * USER_ACTION (engaged) — the outbound WS frame must carry the cid of
 * the LATEST mounted plan, not the first one.
 *
 * On `main` (commit 36cc15f) this test fails because the bare
 * `if (activeIntervention) { return }` guard drops the second and third
 * triggers and the outbound USER_ACTION lacks a cid entirely.
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

function makeTrigger(id: string, cid: string): WSFrame {
    return {
        type: "INTERVENTION_TRIGGER",
        payload: {
            intervention_id: id,
            suggested_actions: [],
            tab_recommendations: { tabs: [], summary: "" },
        },
        timestamp: Date.now() / 1000,
        sequence: 0,
        correlation_id: cid,
    };
}

type BgListener = (
    msg: Record<string, unknown>,
    sender: unknown,
    sendResponse: (resp: unknown) => void,
) => unknown;

async function setup(): Promise<{
    sock: NonNullable<ReturnType<typeof getLatestSocket>>;
    listener: BgListener;
}> {
    vi.resetModules();
    await import("../background");
    await new Promise((r) => setTimeout(r, 0));
    const sock = getLatestSocket();
    if (!sock) throw new Error("WebSocket fake not installed");
    // Background script adds its message listener synchronously on import.
    const fake = globalThis.__cortexChrome;
    expect(fake.runtime.onMessage.__listenerCount()).toBeGreaterThan(0);
    const listener = fake.runtime.onMessage.addListener.mock.calls[0][0] as BgListener;
    return { sock, listener };
}

describe("F16 active-intervention atomic swap", () => {
    beforeEach(() => {
        vi.resetModules();
    });

    it("latest correlation_id wins after a 3-trigger burst", async () => {
        const { sock, listener } = await setup();

        sock.__deliver(makeTrigger("i1", "cid-1"));
        sock.__deliver(makeTrigger("i2", "cid-2"));
        sock.__deliver(makeTrigger("i3", "cid-3"));

        // Now the popup signals USER_ACTION dismissed; outbound WS frame
        // must carry cid-3 (the most-recently mounted plan).
        sock.sent.length = 0; // discard handshake frames
        listener(
            {
                type: "USER_ACTION",
                action: "dismissed",
                intervention_id: "i3",
            },
            undefined,
            () => {},
        );

        const userActionFrames = sock.sent
            .map((s) => JSON.parse(s) as WSFrame)
            .filter((f) => f.type === "USER_ACTION");
        expect(userActionFrames.length).toBeGreaterThan(0);
        const last = userActionFrames[userActionFrames.length - 1];
        expect(last.correlation_id).toBe("cid-3");
    });

    it("synthesises a local cid when the daemon omits one", async () => {
        const { sock, listener } = await setup();

        sock.__deliver({
            type: "INTERVENTION_TRIGGER",
            payload: {
                intervention_id: "legacy",
                suggested_actions: [],
            },
            timestamp: Date.now() / 1000,
            sequence: 0,
        });

        sock.sent.length = 0;
        listener(
            {
                type: "USER_ACTION",
                action: "dismissed",
                intervention_id: "legacy",
            },
            undefined,
            () => {},
        );

        const frames = sock.sent
            .map((s) => JSON.parse(s) as WSFrame)
            .filter((f) => f.type === "USER_ACTION");
        expect(frames.length).toBeGreaterThan(0);
        expect(typeof frames[0].correlation_id).toBe("string");
        expect(frames[0].correlation_id?.length).toBeGreaterThan(0);
    });
});
