/**
 * F07b + F08b: extension presents the capability token on SHUTDOWN and
 * the launcher `/stop` fetch.
 *
 * - getAuthToken() fetches once from the native host and caches the
 *   token in chrome.storage.session.
 * - Subsequent calls hit cache without going back to the native host.
 * - The STOP_CORTEX flow's outbound SHUTDOWN frame carries
 *   payload.auth_token; the /stop fetch carries the X-Cortex-Auth-Token
 *   header.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { getLatestSocket } from "../test/mocks/websocket";

type BgListener = (
    msg: Record<string, unknown>,
    sender: unknown,
    sendResponse: (resp: unknown) => void,
) => unknown;

describe("F07b + F08b extension auth wiring", () => {
    beforeEach(() => {
        vi.resetModules();
    });

    it("getAuthToken hits native host once, caches in session", async () => {
        const { getAuthToken, _resetAuthCache } = await import("../lib/auth");
        const fake = globalThis.__cortexChrome;

        fake.runtime.sendNativeMessage.mockImplementation(
            (
                _app: string,
                _msg: unknown,
                cb?: (resp: unknown) => void,
            ) => {
                if (cb) cb({ auth_token: "deadbeefcafebabe" });
                return Promise.resolve({ auth_token: "deadbeefcafebabe" });
            },
        );

        const t1 = await getAuthToken();
        const t2 = await getAuthToken();
        expect(t1).toBe("deadbeefcafebabe");
        expect(t2).toBe("deadbeefcafebabe");
        expect(fake.runtime.sendNativeMessage).toHaveBeenCalledTimes(1);

        await _resetAuthCache();
        const t3 = await getAuthToken();
        expect(t3).toBe("deadbeefcafebabe");
        expect(fake.runtime.sendNativeMessage).toHaveBeenCalledTimes(2);
    });

    it("STOP_CORTEX attaches auth_token to SHUTDOWN payload and X-Cortex-Auth-Token to /stop", async () => {
        const fake = globalThis.__cortexChrome;
        fake.runtime.sendNativeMessage.mockImplementation(
            (
                _app: string,
                msg: unknown,
                cb?: (resp: unknown) => void,
            ) => {
                const m = msg as { command?: string };
                if (m?.command === "get_auth_token") {
                    if (cb) cb({ auth_token: "tok_abc" });
                    return Promise.resolve({ auth_token: "tok_abc" });
                }
                if (cb) cb({ status: "ok" });
                return Promise.resolve({ status: "ok" });
            },
        );

        const fetchCalls: Array<{ url: string; init?: RequestInit }> = [];
        const realFetch = globalThis.fetch;
        globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
            fetchCalls.push({ url: String(input), init });
            return Promise.resolve(new Response("ok", { status: 200 }));
        }) as typeof fetch;

        try {
            await import("../background");
            await new Promise((r) => setTimeout(r, 0));
            const sock = getLatestSocket();
            expect(sock).not.toBeNull();

            const listener = fake.runtime.onMessage.addListener.mock.calls[0][0] as BgListener;

            const responses: unknown[] = [];
            listener(
                { type: "STOP_CORTEX" },
                undefined,
                (r) => responses.push(r),
            );

            // STOP_CORTEX is async; give the inner queue time to drain.
            // The chain has explicit setTimeout(1000) + setTimeout(500)
            // gates; fake them out by stepping the event loop a few times.
            for (let i = 0; i < 30; i++) {
                await new Promise((r) => setTimeout(r, 100));
                if (responses.length > 0) break;
            }

            const shutdownFrame = sock!.sent
                .map((s) => JSON.parse(s) as { type: string; payload: Record<string, unknown> })
                .find((f) => f.type === "SHUTDOWN");
            expect(shutdownFrame).toBeDefined();
            expect(shutdownFrame!.payload.auth_token).toBe("tok_abc");

            const stopFetch = fetchCalls.find((c) => c.url.includes("/stop"));
            expect(stopFetch).toBeDefined();
            const headers = (stopFetch!.init?.headers as Record<string, string>) || {};
            expect(headers["X-Cortex-Auth-Token"]).toBe("tok_abc");

            const shutdownFetch = fetchCalls.find((c) => c.url.includes("/shutdown"));
            expect(shutdownFetch).toBeDefined();
            const shutdownHeaders =
                (shutdownFetch!.init?.headers as Record<string, string>) || {};
            expect(shutdownHeaders["X-Cortex-Auth-Token"]).toBe("tok_abc");
        } finally {
            globalThis.fetch = realFetch;
        }
    }, 15000);
});
