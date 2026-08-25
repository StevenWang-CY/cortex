/**
 * WP6 containment and migration of legacy ``cortex_auto_focus_state``.
 *
 * Invariants:
 *   1. A legacy START_FOCUS_AUTO frame never writes blocker ownership.
 *   2. STOP_FOCUS_AUTO cannot fabricate cleanup work after a rejected start.
 *   3. When old local storage claims ``autoFocusArmed=true`` but the focus
 *      session was lost in the restart, the boot path clears the bit
 *      and emits a ``auto_focus_inconsistent_state_recovered``
 *      USER_ACTION.
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
    socket.__deliver({
        type: "SETTINGS_SYNC",
        payload: { execution_mode: "authorized" },
        sequence: 2,
        timestamp: Date.now() / 1000,
        correlation_id: null,
        target_client_types: null,
        source_client_type: "daemon",
    });
}

function deliverFrame(payload: Record<string, unknown>): void {
    const socket = getLatestSocket()!;
    socket.__deliver({
        ...payload,
        sequence: payload.sequence ?? 99,
        timestamp: payload.timestamp ?? Date.now() / 1000,
        correlation_id: payload.correlation_id ?? null,
        target_client_types: payload.target_client_types ?? null,
        source_client_type: "daemon",
    });
}

describe("WP6 — legacy auto-focus state containment", () => {
    beforeEach(() => {
        vi.resetModules();
    });

    it("does not persist auto-focus state from a legacy start frame", async () => {
        await bootBackground();
        authenticate();
        const fake = globalThis.__cortexChrome;

        deliverFrame({
            type: "START_FOCUS_AUTO",
            payload: {
                duration_minutes: 25,
                reason: "biometric_hyper",
                preset: "developer",
                custom_domains: [],
            },
        });
        // Wait past the debounce (persistAutoFocusState uses 200ms).
        await new Promise((r) => setTimeout(r, 250));

        const localState = fake.storage.local.__peek();
        const blob = localState.cortex_auto_focus_state as
            | {
                  autoFocusArmed: boolean;
                  _activeFocusPresetName: string;
                  activeFocusPresetPatterns: string[];
              }
            | undefined;
        expect(blob).toBeUndefined();
    });

    it("start then stop leaves no local auto-focus ownership", async () => {
        await bootBackground();
        authenticate();
        const fake = globalThis.__cortexChrome;

        deliverFrame({
            type: "START_FOCUS_AUTO",
            payload: {
                duration_minutes: 20,
                reason: "biometric_hyper",
                preset: "developer",
                custom_domains: [],
            },
        });
        await new Promise((r) => setTimeout(r, 250));
        expect(
            fake.storage.local.__peek().cortex_auto_focus_state,
        ).toBeUndefined();

        deliverFrame({
            type: "STOP_FOCUS_AUTO",
            payload: { reason: "duration_elapsed" },
        });
        await new Promise((r) => setTimeout(r, 250));

        expect(
            fake.storage.local.__peek().cortex_auto_focus_state,
        ).toBeUndefined();
    });

    it(
        "recovers an inconsistent state (armed=true but focusSession=null)",
        async () => {
            // Pre-seed local storage with the inconsistent blob — the
            // session bucket is empty so restoreState leaves focusSession
            // null while local restore tries to set armed=true.
            await bootBackground();
            const fake = globalThis.__cortexChrome;
            fake.storage.local.__reset({
                cortex_auto_focus_state: {
                    autoFocusArmed: true,
                    _activeFocusPresetName: "developer",
                    activeFocusPresetPatterns: ["reddit\\.com"],
                },
            });

            // Re-import background — fresh SW boot reads both buckets.
            vi.resetModules();
            await import("../background");
            // Allow restoreState's async chain to complete.
            await new Promise((r) => setTimeout(r, 50));

            // Authenticate so the USER_ACTION send can fire.
            authenticate();
            await new Promise((r) => setTimeout(r, 100));
            // Wait past persist debounce.
            await new Promise((r) => setTimeout(r, 300));

            // The local mirror should be cleared.
            const blob =
                fake.storage.local.__peek().cortex_auto_focus_state as
                    | { autoFocusArmed: boolean }
                    | undefined;
            // Recovery clears the inconsistent armed bit.
            if (blob !== undefined) {
                expect(blob.autoFocusArmed).toBe(false);
            }
            // The daemon should have received the recovery USER_ACTION
            // (sent over WS after authentication completed).
            const socket = getLatestSocket()!;
            const sent = socket.sent
                .map((s) => {
                    try {
                        return JSON.parse(s);
                    } catch {
                        return null;
                    }
                })
                .filter((m): m is Record<string, unknown> => m !== null);
            const recovery = sent.find(
                (m) =>
                    m.type === "USER_ACTION"
                    && (m.payload as { action?: string } | undefined)?.action
                        === "auto_focus_inconsistent_state_recovered",
            );
            expect(recovery).toBeDefined();
        },
    );
});
