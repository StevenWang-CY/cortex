/**
 * P0 §3.3 — background.ts SESSION_RECAP handler validation gate.
 *
 * Phase 4.A made the daemon's REQUEST_SESSION_RECAP reply payload an
 * empty object (``{}``) when no recap is cached, rather than silently
 * dropping the request. The background script's SESSION_RECAP handler
 * must therefore gate on a present-and-string ``session_id`` field —
 * any payload missing it counts as "no recap to surface" and must NOT:
 *
 *   - cache to ``chrome.storage.local`` (would replay the empty card
 *     on the next popup open),
 *   - light the toolbar action badge (would lie about a waiting recap),
 *   - broadcast SESSION_RECAP_READY to an open popup (would render
 *     an empty card).
 *
 * A valid payload (non-empty ``session_id``) must still flow through
 * the original cache + badge + broadcast path.
 *
 * We drive the contract by importing background.ts (which opens its
 * WebSocket against the fake registry from ``test/setup.ts``),
 * pretending the AUTH handshake is complete, then ``__deliver``-ing
 * SESSION_RECAP frames straight into the dispatch path.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { getLatestSocket } from "../test/mocks/websocket";

async function setup() {
    vi.resetModules();
    process.env.CORTEX_DEBUG = "true";
    await import("../background");
    // Drain the WS open / AUTH microtasks.
    await new Promise((r) => setTimeout(r, 0));
    const sock = getLatestSocket();
    if (!sock) throw new Error("WebSocket fake missing");
    return { sock, fake: globalThis.__cortexChrome };
}

describe("P0 §3.3 — SESSION_RECAP validity gate", () => {
    beforeEach(() => {
        vi.resetModules();
    });

    it("does NOT cache, badge, or broadcast when payload has empty session_id", async () => {
        const { sock, fake } = await setup();
        const setSpy = fake.storage.local.set as ReturnType<typeof vi.fn>;
        const sendMessage = fake.runtime.sendMessage as ReturnType<typeof vi.fn>;
        // Snapshot pre-call state so we can detect that NOTHING new
        // landed as a result of the empty SESSION_RECAP.
        const callsBefore = setSpy.mock.calls.length;
        const broadcastsBefore = sendMessage.mock.calls.length;

        sock.__deliver({
            type: "SESSION_RECAP",
            payload: {},
            timestamp: Date.now() / 1000,
            sequence: 1,
        });
        await new Promise((r) => setTimeout(r, 0));

        // chrome.storage.local.set called for the recap cache? NO.
        const recapCacheWrites = setSpy.mock.calls
            .slice(callsBefore)
            .filter((c) => {
                const obj = c[0] as Record<string, unknown>;
                return obj && "cortex.lastRecap" in obj;
            });
        expect(recapCacheWrites.length).toBe(0);

        // broadcastToPopup(SESSION_RECAP_READY) emitted? NO.
        const recapReadyBroadcasts = sendMessage.mock.calls
            .slice(broadcastsBefore)
            .filter(
                (c) =>
                    (c[0] as { type?: string })?.type ===
                    "SESSION_RECAP_READY",
            );
        expect(recapReadyBroadcasts.length).toBe(0);
    });

    it("does NOT cache, badge, or broadcast when session_id is missing entirely", async () => {
        const { sock, fake } = await setup();
        const setSpy = fake.storage.local.set as ReturnType<typeof vi.fn>;
        const sendMessage = fake.runtime.sendMessage as ReturnType<typeof vi.fn>;
        const callsBefore = setSpy.mock.calls.length;
        const broadcastsBefore = sendMessage.mock.calls.length;

        sock.__deliver({
            type: "SESSION_RECAP",
            payload: {
                // C4 wrapper, but the report deliberately lacks a
                // ``session_id`` — must be treated as "no recap".
                report: {
                    duration_seconds: 1800,
                    flow_percentage: 50,
                },
                generated_at: "2026-05-29T10:00:00Z",
                persisted: true,
            },
            timestamp: Date.now() / 1000,
            sequence: 2,
        });
        await new Promise((r) => setTimeout(r, 0));

        const recapCacheWrites = setSpy.mock.calls
            .slice(callsBefore)
            .filter((c) => {
                const obj = c[0] as Record<string, unknown>;
                return obj && "cortex.lastRecap" in obj;
            });
        expect(recapCacheWrites.length).toBe(0);

        const recapReadyBroadcasts = sendMessage.mock.calls
            .slice(broadcastsBefore)
            .filter(
                (c) =>
                    (c[0] as { type?: string })?.type ===
                    "SESSION_RECAP_READY",
            );
        expect(recapReadyBroadcasts.length).toBe(0);
    });

    it("does NOT cache or broadcast when session_id is the empty string", async () => {
        const { sock, fake } = await setup();
        const setSpy = fake.storage.local.set as ReturnType<typeof vi.fn>;
        const sendMessage = fake.runtime.sendMessage as ReturnType<typeof vi.fn>;
        const callsBefore = setSpy.mock.calls.length;
        const broadcastsBefore = sendMessage.mock.calls.length;

        sock.__deliver({
            type: "SESSION_RECAP",
            // C4 wrapper with an empty-string report.session_id.
            payload: { report: { session_id: "" }, persisted: true },
            timestamp: Date.now() / 1000,
            sequence: 3,
        });
        await new Promise((r) => setTimeout(r, 0));

        const recapCacheWrites = setSpy.mock.calls
            .slice(callsBefore)
            .filter((c) => {
                const obj = c[0] as Record<string, unknown>;
                return obj && "cortex.lastRecap" in obj;
            });
        expect(recapCacheWrites.length).toBe(0);
        const recapReadyBroadcasts = sendMessage.mock.calls
            .slice(broadcastsBefore)
            .filter(
                (c) =>
                    (c[0] as { type?: string })?.type ===
                    "SESSION_RECAP_READY",
            );
        expect(recapReadyBroadcasts.length).toBe(0);
    });

    it("DOES cache and broadcast when session_id is a non-empty string", async () => {
        const { sock, fake } = await setup();
        const setSpy = fake.storage.local.set as ReturnType<typeof vi.fn>;
        const sendMessage = fake.runtime.sendMessage as ReturnType<typeof vi.fn>;

        sock.__deliver({
            type: "SESSION_RECAP",
            // C4 wrapper: report nested under ``report``, plus the
            // ``generated_at`` / ``persisted`` envelope fields.
            payload: {
                report: {
                    session_id: "test-session-valid-001",
                    duration_seconds: 1800,
                    flow_percentage: 67,
                    breaks_taken: 2,
                    longest_flow_streak_seconds: 600,
                    avg_hr_bpm: 71,
                },
                generated_at: "2026-05-29T10:00:00Z",
                persisted: true,
            },
            timestamp: Date.now() / 1000,
            sequence: 4,
        });
        // The valid branch fans out through chrome.storage.local.get
        // (to look up the dismissedRecapSessionId) before writing —
        // poll a couple of microtask turns so the get callback can run.
        for (let i = 0; i < 5; i++) {
            await new Promise((r) => setTimeout(r, 0));
        }

        const recapCacheWrites = setSpy.mock.calls.filter((c) => {
            const obj = c[0] as Record<string, unknown>;
            return obj && "cortex.lastRecap" in obj;
        });
        expect(recapCacheWrites.length).toBeGreaterThanOrEqual(1);

        const recapReadyBroadcasts = sendMessage.mock.calls.filter(
            (c) =>
                (c[0] as { type?: string })?.type === "SESSION_RECAP_READY",
        );
        expect(recapReadyBroadcasts.length).toBeGreaterThanOrEqual(1);
    });
});
