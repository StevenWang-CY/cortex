/**
 * Audit Wave-2 follow-up: extension surfaces unhandled-but-known WS frames.
 *
 * ``cortex/libs/schemas/ws_message_types.py`` registers wire types the
 * daemon may emit, including nine LEETCODE_* cues
 * (``LEETCODE_LOCK_EDITOR`` / ``LEETCODE_INTERCEPT_SUBMIT`` /
 * ``LEETCODE_GATE_SOLUTIONS`` / ``LEETCODE_SHOW_SESSION_BRIEFING`` and
 * five ``LEETCODE_AI_*`` checks) that no active runtime emitter calls
 * today. Pre-fix, ``background.ts``'s message switch had no default
 * branch — any unrecognised but schema-valid frame was silently dropped.
 * That made a future regression (extension drifts behind a new
 * daemon-side emit) invisible.
 *
 * The defensive default arm now logs through ``console.warn`` so the
 * developer-console DEBUG flag surfaces the dropped frame. This test
 * pins the contract: a schema-valid frame whose ``type`` has no handler
 * MUST hit the default arm (visible via the console.warn spy) instead
 * of going to ``broadcastToPopup`` or ``broadcastToContentScripts``.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { getLatestSocket } from "../test/mocks/websocket";

async function setup() {
    vi.resetModules();
    await import("../background");
    await new Promise((r) => setTimeout(r, 0));
    const sock = getLatestSocket();
    if (!sock) throw new Error("WebSocket fake missing");
    return { sock };
}

describe("audit-w2 unhandled WS frame default arm", () => {
    beforeEach(() => {
        vi.resetModules();
        vi.useRealTimers();
        // Ensure DEBUG-gated ``console.warn`` fires in the spec
        // environment. ``background.ts`` reads CORTEX_DEBUG off both
        // ``import.meta.env`` and ``process.env`` at module init, so
        // setting the env var before the dynamic import inside
        // ``setup()`` is sufficient.
        process.env.CORTEX_DEBUG = "true";
    });

    it("logs (does not throw on) a schema-valid known-but-unhandled LEETCODE_AI frame", async () => {
        const { sock } = await setup();
        const warn = vi.spyOn(console, "warn").mockImplementation(() => {});

        // ``LEETCODE_AI_RESTATEMENT_CHECK`` is in the MessageType enum
        // and round-trips through the WSMessage schema — but the
        // extension has no handler for it today. The default arm must
        // catch it.
        sock.__deliver({
            type: "LEETCODE_AI_RESTATEMENT_CHECK",
            payload: { problem_id: "two-sum" },
            timestamp: Date.now() / 1000,
            sequence: 1,
        });

        await new Promise((r) => setTimeout(r, 0));

        const calls = warn.mock.calls.map((args) => args.join(" "));
        const matched = calls.some(
            (line) =>
                line.includes("WS frame with no handler") &&
                line.includes("LEETCODE_AI_RESTATEMENT_CHECK"),
        );
        expect(matched).toBe(true);

        warn.mockRestore();
    });

    it("does not log for a handled frame (STATE_UPDATE)", async () => {
        const { sock } = await setup();
        const warn = vi.spyOn(console, "warn").mockImplementation(() => {});

        sock.__deliver({
            type: "STATE_UPDATE",
            payload: {
                state: "FLOW",
                confidence: 0.7,
                scores: {},
                signal_quality: {},
                dwell_seconds: 0,
                reasons: [],
            },
            timestamp: Date.now() / 1000,
            sequence: 1,
        });

        await new Promise((r) => setTimeout(r, 0));

        const calls = warn.mock.calls.map((args) => args.join(" "));
        const matched = calls.some((line) =>
            line.includes("WS frame with no handler"),
        );
        expect(matched).toBe(false);

        warn.mockRestore();
    });
});
