/**
 * Phase 4d Task G — global keyboard shortcuts wired via
 * ``chrome.commands.onCommand``.
 *
 * Verifies the three commands declared in package.json route to the
 * canonical WS frame / native_host call.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { getLatestSocket } from "../test/mocks/websocket";

async function bootBackground(): Promise<void> {
    vi.resetModules();
    process.env.CORTEX_DEBUG = "true";
    await import("../background");
    await new Promise((r) => setTimeout(r, 0));
}

function authenticate(): void {
    const socket = getLatestSocket()!;
    socket.__open();
    socket.__deliver({
        type: "AUTH_OK",
        payload: {},
        sequence: 1,
        timestamp: Date.now() / 1000,
        correlation_id: null,
        target_client_types: null,
        source_client_type: "daemon",
    });
}

describe("Phase 4d Task G — chrome.commands listener", () => {
    beforeEach(() => {
        vi.resetModules();
    });

    it("registers the onCommand listener at boot", async () => {
        await bootBackground();
        const fake = globalThis.__cortexChrome;
        expect(fake.commands.onCommand.__listenerCount()).toBeGreaterThan(0);
    });

    it("pause-cortex emits a QUIET_MODE_TOGGLE wire frame", async () => {
        await bootBackground();
        authenticate();
        const fake = globalThis.__cortexChrome;
        const socket = getLatestSocket()!;
        const sentBefore = socket.sent.length;

        fake.commands.onCommand.__dispatch("pause-cortex");
        await new Promise((r) => setTimeout(r, 50));

        const newSent = socket.sent
            .slice(sentBefore)
            .map((s) => {
                try { return JSON.parse(s); } catch { return null; }
            })
            .filter((m): m is Record<string, unknown> => m !== null);
        const frame = newSent.find((m) => m.type === "QUIET_MODE_TOGGLE");
        expect(frame).toBeDefined();
        const payload = frame!.payload as { kind: string; source: string };
        expect(payload.source).toBe("shortcut");
        // First toggle from default off → pause.
        expect(payload.kind).toBe("pause");
    });

    it(
        "dismiss-overlay emits a USER_ACTION with action=dismiss_overlay",
        async () => {
            await bootBackground();
            authenticate();
            const fake = globalThis.__cortexChrome;
            const socket = getLatestSocket()!;
            const sentBefore = socket.sent.length;

            fake.commands.onCommand.__dispatch("dismiss-overlay");
            await new Promise((r) => setTimeout(r, 50));

            const newSent = socket.sent
                .slice(sentBefore)
                .map((s) => {
                    try { return JSON.parse(s); } catch { return null; }
                })
                .filter((m): m is Record<string, unknown> => m !== null);
            const frame = newSent.find(
                (m) =>
                    m.type === "USER_ACTION"
                    && (m.payload as { action?: string } | undefined)?.action
                        === "dismiss_overlay",
            );
            expect(frame).toBeDefined();
        },
    );

    it("view-history relays through chrome.runtime.sendNativeMessage", async () => {
        await bootBackground();
        const fake = globalThis.__cortexChrome;
        const before = fake.runtime.sendNativeMessage.mock.calls.length;

        fake.commands.onCommand.__dispatch("view-history");
        await new Promise((r) => setTimeout(r, 20));

        expect(fake.runtime.sendNativeMessage.mock.calls.length).toBeGreaterThan(
            before,
        );
        const call =
            fake.runtime.sendNativeMessage.mock.calls[before];
        expect(call[0]).toBe("com.cortex.launcher");
        expect(call[1]).toEqual({
            command: "raise_dashboard",
            target: "history",
        });
    });
});
