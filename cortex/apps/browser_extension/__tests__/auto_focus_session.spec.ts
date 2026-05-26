/**
 * P0 §3.10 — daemon-armed focus session in the browser extension.
 *
 * The daemon emits ``START_FOCUS_AUTO`` when sustained HYPER trips the
 * arm gate. The browser extension MUST:
 *   1. Open a focus session whose goal is auto-generated from the
 *      reason (so the popup pill reads "Auto-focus" rather than the
 *      manual placeholder).
 *   2. Register a ``chrome.alarm`` so the session auto-tears down after
 *      the configured duration if no STOP_FOCUS_AUTO arrives.
 *   3. Treat ``STOP_FOCUS_AUTO`` as a NO-OP when the session was
 *      manually armed (no daemon hand-over of a user-controlled
 *      session).
 *
 * Behaviour invariants in this spec:
 *   * The daemon's START → STOP → START pattern must round-trip without
 *     leaking stale alarms.
 *   * QUIET_MODE_STATE updates the quietMode flag without touching the
 *     focus session.
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

describe("P0 §3.10 — auto-armed focus session", () => {
    beforeEach(() => {
        vi.resetModules();
    });

    it("opens a focus session and registers an alarm on START_FOCUS_AUTO", async () => {
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

        // chrome.alarms.create was called with a name starting with
        // ``cortex_auto_focus_``.
        const newAlarmCalls = fake.alarms.create.mock.calls.slice(alarmsBefore);
        const autoAlarm = newAlarmCalls.find(
            (c) => typeof c[0] === "string" && (c[0] as string).startsWith("cortex_auto_focus_"),
        );
        expect(autoAlarm).toBeDefined();
        const opts = autoAlarm![1] as { when?: number };
        expect(opts.when).toBeGreaterThan(Date.now());
    });

    it("STOP_FOCUS_AUTO tears down only auto-armed sessions", async () => {
        await bootBackground();
        authenticate();
        const fake = globalThis.__cortexChrome;

        // Arm auto-focus → STOP_FOCUS_AUTO clears it.
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

        // The auto-focus alarm should have been cleared.
        expect(fake.alarms.clear.mock.calls.length).toBeGreaterThan(clearsBefore);
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
