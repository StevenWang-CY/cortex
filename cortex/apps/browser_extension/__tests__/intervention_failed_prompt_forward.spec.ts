/**
 * P1-FC-INTERVENTION-FAILED / P1-FC-INTERVENTION-PROMPT — background.ts
 * must forward these daemon broadcasts to the popup.
 *
 * Before this fix neither message had a consumer on the browser surface:
 *   - INTERVENTION_FAILED (a TOTAL mutation failure where the workspace
 *     was NOT changed) fell through the dispatch switch's debug default
 *     and was silently dropped — the user never learned the apply failed.
 *   - INTERVENTION_PROMPT (cross-surface micro-commit / movement-break
 *     sync) was consumed on the desktop overlay but DROPPED on the
 *     browser, so a popup-open user got no awareness of the prompt.
 *
 * Contract: delivering each WS frame must call ``broadcastToPopup`` (i.e.
 * ``chrome.runtime.sendMessage``) with a matching ``{type, payload}``.
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
    // Drive AUTH to completion so the dispatch path is live.
    sock.__deliver({
        type: "AUTH_OK",
        payload: { ok: true },
        timestamp: Date.now() / 1000,
        sequence: 0,
    });
    for (let i = 0; i < 5; i++) {
        await new Promise((r) => setTimeout(r, 0));
    }
    return { sock, fake: globalThis.__cortexChrome };
}

describe("P1-FC INTERVENTION_FAILED / INTERVENTION_PROMPT forwarding", () => {
    beforeEach(() => {
        vi.resetModules();
    });

    it("forwards INTERVENTION_FAILED to the popup", async () => {
        const { sock, fake } = await setup();
        const sendMessage = fake.runtime.sendMessage as ReturnType<typeof vi.fn>;
        const before = sendMessage.mock.calls.length;

        const payload = {
            intervention_id: "iv-failed-1",
            error_reason: "Extension lacks tab permission",
            failed_action_types: ["close_tab", "mute_tab"],
        };
        sock.__deliver({
            type: "INTERVENTION_FAILED",
            payload,
            timestamp: Date.now() / 1000,
            sequence: 1,
        });
        await new Promise((r) => setTimeout(r, 0));

        const forwarded = sendMessage.mock.calls
            .slice(before)
            .map((c) => c[0] as { type?: string; payload?: unknown })
            .find((m) => m.type === "INTERVENTION_FAILED");
        expect(forwarded).toBeDefined();
        expect(forwarded!.payload).toMatchObject(payload);
    });

    it("forwards INTERVENTION_PROMPT to the popup", async () => {
        const { sock, fake } = await setup();
        const sendMessage = fake.runtime.sendMessage as ReturnType<typeof vi.fn>;
        const before = sendMessage.mock.calls.length;

        const payload = {
            action_type: "prompt_micro_commit",
            prompt: "Commit the one line you just changed?",
            timeout_seconds: 120,
            metadata: {},
        };
        sock.__deliver({
            type: "INTERVENTION_PROMPT",
            payload,
            timestamp: Date.now() / 1000,
            sequence: 2,
        });
        await new Promise((r) => setTimeout(r, 0));

        const forwarded = sendMessage.mock.calls
            .slice(before)
            .map((c) => c[0] as { type?: string; payload?: unknown })
            .find((m) => m.type === "INTERVENTION_PROMPT");
        expect(forwarded).toBeDefined();
        expect(forwarded!.payload).toMatchObject(payload);
    });
});
