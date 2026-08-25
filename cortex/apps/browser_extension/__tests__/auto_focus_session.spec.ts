/**
 * WP6 containment — legacy daemon-armed focus commands are inert.
 *
 * A physiological state transition is neither explicit user consent nor an
 * exact manifest-bound capability. START_FOCUS_AUTO therefore cannot arm a
 * blocker or create an alarm. STOP_FOCUS_AUTO remains a cleanup-only no-op
 * when Cortex owns no auto-armed session.
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
    // Auto-focus is an applying path and is deliberately unavailable in
    // the production default. This suite opts into authorized mode so it
    // can test the feature's own lifecycle independently of containment.
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

describe("WP6 — legacy auto-focus containment", () => {
    beforeEach(() => {
        vi.resetModules();
    });

    it("does not arm or register an alarm on START_FOCUS_AUTO", async () => {
        await bootBackground();
        authenticate();

        const fake = globalThis.__cortexChrome;
        const alarmsBefore = fake.alarms.create.mock.calls.length;

        deliverFrame({
            type: "START_FOCUS_AUTO",
            payload: {
                duration_minutes: 20,
                reason: "biometric_hyper",
                preset: "developer",
                custom_domains: [],
            },
        });
        await new Promise((r) => setTimeout(r, 0));

        const newAlarmCalls = fake.alarms.create.mock.calls.slice(alarmsBefore);
        const autoAlarm = newAlarmCalls.find(
            (c) => typeof c[0] === "string" && (c[0] as string).startsWith("cortex_auto_focus_"),
        );
        expect(autoAlarm).toBeUndefined();
    });

    it("START then STOP cannot manufacture auto-focus ownership", async () => {
        await bootBackground();
        authenticate();
        const fake = globalThis.__cortexChrome;

        deliverFrame({
            type: "START_FOCUS_AUTO",
            payload: {
                duration_minutes: 5,
                reason: "biometric_hyper",
                preset: "writer",
                custom_domains: [],
            },
        });
        await new Promise((r) => setTimeout(r, 0));
        const clearsBefore = fake.alarms.clear.mock.calls.length;

        deliverFrame({
            type: "STOP_FOCUS_AUTO",
            payload: { reason: "sustained_recovery" },
        });
        await new Promise((r) => setTimeout(r, 0));

        expect(fake.alarms.clear.mock.calls.length).toBe(clearsBefore);
    });

    it("QUIET_MODE_STATE broadcasts update the popup pill", async () => {
        await bootBackground();
        authenticate();
        const fake = globalThis.__cortexChrome;

        // Capture sendMessage(popup) calls.
        const popupSends: Array<Record<string, unknown>> = [];
        fake.runtime.sendMessage.mockImplementation(
            (msg: Record<string, unknown>) => {
                popupSends.push(msg);
                return Promise.resolve(undefined);
            },
        );

        deliverFrame({
            type: "QUIET_MODE_STATE",
            payload: { kind: "snooze_15", duration_minutes: 15, ends_at: 0, source: "overlay" },
        });
        await new Promise((r) => setTimeout(r, 0));

        const relayed = popupSends.find(
            (m) => m.type === "QUIET_MODE_STATE",
        );
        expect(relayed).toBeDefined();
        const payload = relayed!.payload as Record<string, unknown>;
        expect(payload.kind).toBe("snooze_15");
    });
});
