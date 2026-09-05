/**
 * Native-host reachability is cached: socket churn while the app is closed
 * must not spawn the host process; only popup open / install force a fresh
 * probe. Also covers the sticky stop intent (no reconnect from the keepalive
 * alarm until Start) and the handshake verdict clearing on a healthy open.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { NativeHostStatusCache } from "../lib/native-host-status";
import { getAllSockets, getLatestSocket } from "../test/mocks/websocket";

type Listener = (
    message: Record<string, unknown>,
    sender: unknown,
    sendResponse: (response: unknown) => void,
) => unknown;

function statusProbes(): number {
    return globalThis.__cortexChrome.runtime.sendNativeMessage.mock.calls
        .filter((call) => (call[1] as { command?: string })?.command === "status")
        .length;
}

function diagnostics(): Array<Record<string, unknown>> {
    return globalThis.__cortexChrome.runtime.sendMessage.mock.calls
        .map((call) => call[0] as { type?: string; payload?: Record<string, unknown> })
        .filter((m) => m.type === "CONNECTIVITY_DIAGNOSTIC")
        .map((m) => m.payload ?? {});
}

async function settle(): Promise<void> {
    for (let i = 0; i < 8; i++) await new Promise((r) => setTimeout(r, 0));
}

describe("NativeHostStatusCache", () => {
    it("answers from cache inside the TTL, re-probes when forced or stale, and shares in-flight probes", async () => {
        const send = vi.fn(async () => ({ command: "status" as const, status: "stopped" as const }));
        const cache = new NativeHostStatusCache(5_000, send as never);
        const [a, b] = await Promise.all([cache.probe(false, 0), cache.probe(false, 0)]);
        expect(a.status).toBe("present");
        expect(b).toBe(a);
        expect(send).toHaveBeenCalledTimes(1);
        await cache.probe(false, 1_000);
        expect(send).toHaveBeenCalledTimes(1);
        await cache.probe(true, 1_000);
        expect(send).toHaveBeenCalledTimes(2);
        await cache.probe(false, Date.now() + 10_000);
        expect(send).toHaveBeenCalledTimes(3);
    });

    it("reports a missing host without throwing", async () => {
        const send = vi.fn(async () => { throw new Error("Specified native messaging host not found"); });
        const cache = new NativeHostStatusCache(5_000, send as never);
        const result = await cache.probe(true);
        expect(result.status).toBe("missing");
        expect(result.error).toContain("not found");
    });
});

describe("worker connectivity behaviour", () => {
    beforeEach(() => {
        vi.resetModules();
        (globalThis as unknown as { fetch: unknown }).fetch = vi.fn().mockResolvedValue({
            ok: true,
            json: () => Promise.resolve({ version: "0.3.15" }),
        });
    });

    it("does not spawn the native host on socket churn; popup open forces one probe", async () => {
        await import("../background");
        await settle();
        const afterBoot = statusProbes();
        expect(afterBoot).toBeLessThanOrEqual(1);

        for (let i = 0; i < 3; i++) {
            getLatestSocket()!.__remoteClose(1006, "app closed");
            await settle();
        }
        expect(statusProbes()).toBe(afterBoot);

        const listener = globalThis.__cortexChrome.runtime.onMessage.addListener.mock
            .calls[0][0] as Listener;
        listener({ type: "REQUEST_CONNECTIVITY_DIAGNOSTIC" }, {}, () => undefined);
        await settle();
        expect(statusProbes()).toBe(afterBoot + 1);
    });

    it("clears the handshake verdict on a healthy open and only claims one after a policy close", async () => {
        await import("../background");
        await settle();
        globalThis.__cortexChrome.runtime.sendMessage.mockClear();

        // Socket that opened, then was refused by the daemon with a policy code.
        getLatestSocket()!.__remoteClose(1011, "auth rejected");
        await settle();
        const rejected = diagnostics().at(-1);
        expect(rejected?.handshake_error).toBe("handshake_rejected");

        // Reconnect now (Start): the new socket auto-opens and the healthy
        // open must clear the verdict.
        const listener = globalThis.__cortexChrome.runtime.onMessage.addListener.mock
            .calls[0][0] as Listener;
        listener({ type: "CONNECT" }, {}, () => undefined);
        await settle();
        const socket = getLatestSocket()!;
        if (socket.readyState === WebSocket.CONNECTING) socket.__open();
        await settle();
        const healthy = diagnostics().at(-1);
        expect(healthy?.handshake_error).toBeNull();

        // A plain drop (never opened, or a network close) is never a rejection.
        getLatestSocket()!.__remoteClose(1006, "network");
        await settle();
        expect(diagnostics().at(-1)?.handshake_error).toBeNull();
    });

    it("keeps a stop sticky until the user presses Start", async () => {
        await import("../background");
        await settle();
        const listener = globalThis.__cortexChrome.runtime.onMessage.addListener.mock
            .calls[0][0] as Listener;
        listener({ type: "STOP_CORTEX" }, {}, () => undefined);
        // The stop chain flushes SHUTDOWN for 300-500 ms before it closes
        // the socket; wait that out so the keepalive check below is real.
        await new Promise((r) => setTimeout(r, 800));
        await settle();
        const socketsAfterStop = getAllSockets().length;

        globalThis.__cortexChrome.alarms.onAlarm.__dispatch({ name: "cortex-keepalive", scheduledTime: Date.now() });
        await settle();
        expect(getAllSockets().length).toBe(socketsAfterStop);
        expect(globalThis.__cortexChrome.storage.session.__peek().cortex_stop_requested).toBe(true);

        let state: { stopRequested?: boolean } | undefined;
        listener({ type: "GET_STATE" }, {}, (r) => { state = r as { stopRequested?: boolean }; });
        await settle();
        expect(state?.stopRequested).toBe(true);

        listener({ type: "CONNECT" }, {}, () => undefined);
        await settle();
        expect(getAllSockets().length).toBe(socketsAfterStop + 1);
        expect(globalThis.__cortexChrome.storage.session.__peek().cortex_stop_requested).toBeUndefined();
    });
});
