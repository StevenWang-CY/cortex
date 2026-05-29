/**
 * C1 — /api/cost and /api/feedback stay capability-token-gated.
 *
 * EXT-4: the GET_COST background proxy and the popup's /api/feedback POST
 * previously sent NO ``X-Cortex-Auth-Token`` header, so the gateway 401'd
 * every request. This test pins that the background GET_COST proxy now
 * fetches the cached capability token (via the same native-host path the
 * STOP chain uses) and attaches it as ``X-Cortex-Auth-Token``.
 *
 * (The popup-side /api/feedback header attachment shares the exact same
 * ``getAuthToken`` mechanism — see lib/auth + popup.tsx ~handleBugReport.)
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { getLatestSocket } from "../test/mocks/websocket";

type BgListener = (
    msg: Record<string, unknown>,
    sender: unknown,
    sendResponse: (resp: unknown) => void,
) => unknown;

describe("C1 — /api/cost token gating", () => {
    beforeEach(() => {
        vi.resetModules();
    });

    it("GET_COST attaches X-Cortex-Auth-Token from the cached token", async () => {
        const fake = globalThis.__cortexChrome;
        fake.runtime.sendNativeMessage.mockImplementation(
            (
                _app: string,
                msg: unknown,
                cb?: (resp: unknown) => void,
            ) => {
                const m = msg as { command?: string };
                if (m?.command === "get_auth_token") {
                    if (cb) cb({ auth_token: "tok_cost_42" });
                    return Promise.resolve({ auth_token: "tok_cost_42" });
                }
                if (cb) cb({ status: "ok" });
                return Promise.resolve({ status: "ok" });
            },
        );

        const fetchCalls: Array<{ url: string; init?: RequestInit }> = [];
        const realFetch = globalThis.fetch;
        globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
            fetchCalls.push({ url: String(input), init });
            return Promise.resolve(
                new Response(JSON.stringify({ total_usd: 0.12 }), {
                    status: 200,
                    headers: { "Content-Type": "application/json" },
                }),
            );
        }) as typeof fetch;

        try {
            await import("../background");
            await new Promise((r) => setTimeout(r, 0));
            expect(getLatestSocket()).not.toBeNull();

            const listener = fake.runtime.onMessage.addListener.mock
                .calls[0][0] as BgListener;

            const responses: unknown[] = [];
            listener({ type: "GET_COST" }, undefined, (r) =>
                responses.push(r),
            );

            for (let i = 0; i < 20; i++) {
                await new Promise((r) => setTimeout(r, 10));
                if (responses.length > 0) break;
            }

            const costFetch = fetchCalls.find((c) =>
                c.url.includes("/api/cost"),
            );
            expect(costFetch).toBeDefined();
            const headers =
                (costFetch!.init?.headers as Record<string, string>) || {};
            expect(headers["X-Cortex-Auth-Token"]).toBe("tok_cost_42");

            // The proxy still returns the parsed cost body.
            expect(responses.length).toBeGreaterThan(0);
            const resp = responses[0] as {
                ok?: boolean;
                cost?: { total_usd?: number };
            };
            expect(resp.ok).toBe(true);
            expect(resp.cost?.total_usd).toBeCloseTo(0.12);
        } finally {
            globalThis.fetch = realFetch;
        }
    }, 15000);
});
