/**
 * P0 §3.3 — DISMISS_RECAP remembers session_id; re-broadcast of the
 * same session is suppressed.
 *
 * When the user clicks "Dismiss" in the popup recap card, the
 * background script stores the dismissed ``session_id`` in
 * ``chrome.storage.local`` under ``cortex.dismissedRecapSessionId``.
 * If the daemon subsequently re-broadcasts the same SESSION_RECAP
 * (e.g. because the extension reconnected and the on-connect
 * REQUEST_SESSION_RECAP handshake fired), the SESSION_RECAP handler
 * must look up the dismissed id and skip cache + badge + broadcast.
 *
 * A SESSION_RECAP for a DIFFERENT session_id must still flow through
 * the original path — dismissing session A does not silence session B.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { getLatestSocket } from "../test/mocks/websocket";

async function setup() {
    vi.resetModules();
    process.env.CORTEX_DEBUG = "true";
    await import("../background");
    await new Promise((r) => setTimeout(r, 0));
    const sock = getLatestSocket();
    if (!sock) throw new Error("WebSocket fake missing");
    return { sock, fake: globalThis.__cortexChrome };
}

/**
 * Locate the chrome.runtime.onMessage listener registered by
 * background.ts so we can dispatch a DISMISS_RECAP synchronously
 * and inspect the side effects.
 */
function dispatchPopupMessage(
    type: string,
    extra: Record<string, unknown> = {},
): Promise<unknown> {
    const fake = globalThis.__cortexChrome;
    const calls = fake.runtime.onMessage.addListener.mock.calls;
    if (calls.length === 0) {
        return Promise.reject(new Error("no onMessage listener registered"));
    }
    return new Promise((resolve) => {
        const listener = calls[calls.length - 1][0] as (
            msg: Record<string, unknown>,
            sender: Record<string, unknown>,
            sendResponse: (resp?: unknown) => void,
        ) => boolean | undefined;
        listener({ type, ...extra }, {}, (resp) => resolve(resp));
    });
}

describe("P0 §3.3 — dismissedRecapSessionId suppression", () => {
    beforeEach(() => {
        vi.resetModules();
    });

    it("DISMISS_RECAP writes the current session_id under cortex.dismissedRecapSessionId", async () => {
        const { sock, fake } = await setup();

        // Seed a valid recap so DISMISS_RECAP has something to read.
        sock.__deliver({
            type: "SESSION_RECAP",
            // C4 wrapper shape.
            payload: {
                report: {
                    session_id: "dismiss-me-001",
                    duration_seconds: 300,
                },
                persisted: true,
            },
            timestamp: Date.now() / 1000,
            sequence: 1,
        });
        for (let i = 0; i < 5; i++) {
            await new Promise((r) => setTimeout(r, 0));
        }

        // Dispatch DISMISS_RECAP through the popup-facing handler.
        await dispatchPopupMessage("DISMISS_RECAP");
        for (let i = 0; i < 5; i++) {
            await new Promise((r) => setTimeout(r, 0));
        }

        const stored = fake.storage.local.__peek();
        expect(stored["cortex.dismissedRecapSessionId"]).toBe("dismiss-me-001");
        // Cache + timestamp must be removed.
        expect(stored["cortex.lastRecap"]).toBeUndefined();
        expect(stored["cortex.lastRecapTimestamp"]).toBeUndefined();
    });

    it("a re-broadcast of the dismissed session_id is suppressed", async () => {
        const { sock, fake } = await setup();
        const setSpy = fake.storage.local.set as ReturnType<typeof vi.fn>;
        const sendMessage = fake.runtime.sendMessage as ReturnType<
            typeof vi.fn
        >;

        // 1. Seed a recap + dismiss it.
        sock.__deliver({
            type: "SESSION_RECAP",
            payload: {
                report: {
                    session_id: "ghost-session-7",
                    duration_seconds: 600,
                },
                persisted: true,
            },
            timestamp: Date.now() / 1000,
            sequence: 1,
        });
        for (let i = 0; i < 5; i++) {
            await new Promise((r) => setTimeout(r, 0));
        }
        await dispatchPopupMessage("DISMISS_RECAP");
        for (let i = 0; i < 5; i++) {
            await new Promise((r) => setTimeout(r, 0));
        }

        // 2. Snapshot the call counts AFTER dismiss so we can detect
        //    new writes / broadcasts triggered by the re-broadcast.
        const setCallsBefore = setSpy.mock.calls.length;
        const broadcastsBefore = sendMessage.mock.calls.length;

        // 3. Daemon re-broadcasts the SAME session_id (simulates the
        //    on-connect REQUEST_SESSION_RECAP handshake).
        sock.__deliver({
            type: "SESSION_RECAP",
            payload: {
                report: {
                    session_id: "ghost-session-7",
                    duration_seconds: 600,
                },
                persisted: true,
            },
            timestamp: Date.now() / 1000,
            sequence: 2,
        });
        for (let i = 0; i < 5; i++) {
            await new Promise((r) => setTimeout(r, 0));
        }

        // The re-broadcast must NOT have produced a new cache write
        // OR a new SESSION_RECAP_READY broadcast.
        const newCacheWrites = setSpy.mock.calls
            .slice(setCallsBefore)
            .filter((c) => {
                const obj = c[0] as Record<string, unknown>;
                return obj && "cortex.lastRecap" in obj;
            });
        expect(newCacheWrites.length).toBe(0);

        const newBroadcasts = sendMessage.mock.calls
            .slice(broadcastsBefore)
            .filter(
                (c) =>
                    (c[0] as { type?: string })?.type ===
                    "SESSION_RECAP_READY",
            );
        expect(newBroadcasts.length).toBe(0);
    });

    it("a different session_id is still accepted after a dismiss", async () => {
        const { sock, fake } = await setup();
        const setSpy = fake.storage.local.set as ReturnType<typeof vi.fn>;
        const sendMessage = fake.runtime.sendMessage as ReturnType<
            typeof vi.fn
        >;

        // 1. Seed + dismiss session A.
        sock.__deliver({
            type: "SESSION_RECAP",
            payload: {
                report: {
                    session_id: "session-A",
                    duration_seconds: 300,
                },
                persisted: true,
            },
            timestamp: Date.now() / 1000,
            sequence: 1,
        });
        for (let i = 0; i < 5; i++) {
            await new Promise((r) => setTimeout(r, 0));
        }
        await dispatchPopupMessage("DISMISS_RECAP");
        for (let i = 0; i < 5; i++) {
            await new Promise((r) => setTimeout(r, 0));
        }

        // Snapshot for clean diffs.
        const setCallsBefore = setSpy.mock.calls.length;
        const broadcastsBefore = sendMessage.mock.calls.length;

        // 2. Daemon broadcasts a DIFFERENT session.
        sock.__deliver({
            type: "SESSION_RECAP",
            payload: {
                report: {
                    session_id: "session-B-fresh",
                    duration_seconds: 900,
                    flow_percentage: 71,
                },
                persisted: true,
            },
            timestamp: Date.now() / 1000,
            sequence: 2,
        });
        for (let i = 0; i < 5; i++) {
            await new Promise((r) => setTimeout(r, 0));
        }

        // session-B should land — cache write AND broadcast.
        const newCacheWrites = setSpy.mock.calls
            .slice(setCallsBefore)
            .filter((c) => {
                const obj = c[0] as Record<string, unknown>;
                return obj && "cortex.lastRecap" in obj;
            });
        expect(newCacheWrites.length).toBeGreaterThanOrEqual(1);

        const newBroadcasts = sendMessage.mock.calls
            .slice(broadcastsBefore)
            .filter(
                (c) =>
                    (c[0] as { type?: string })?.type ===
                    "SESSION_RECAP_READY",
            );
        expect(newBroadcasts.length).toBeGreaterThanOrEqual(1);
    });
});
