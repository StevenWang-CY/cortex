/**
 * C1 + C2 — popup /api/feedback POSTs carry the capability token.
 *
 * The popup drains its ``pending_feedback`` queue on mount by re-POSTing
 * each item to the token-gated ``/api/feedback`` endpoint. Before the C1
 * fix the drain sent no ``X-Cortex-Auth-Token`` header, so every queued
 * item 401'd forever. This test seeds one queued item, mounts the popup,
 * and asserts the drain fetch attaches the cached capability token.
 */

import React from "react";
import { createRoot } from "react-dom/client";
import { act } from "react-dom/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import CortexPopup from "../popup";

describe("C1/C2 — popup feedback drain auth header", () => {
    let realFetch: typeof globalThis.fetch;

    beforeEach(() => {
        vi.resetModules();
        realFetch = globalThis.fetch;
    });

    afterEach(() => {
        globalThis.fetch = realFetch;
    });

    it("re-POSTs a queued feedback item with X-Cortex-Auth-Token", async () => {
        const fake = globalThis.__cortexChrome;
        // Native host hands back the capability token.
        fake.runtime.sendNativeMessage.mockImplementation(
            (_app: string, msg: unknown, cb?: (resp: unknown) => void) => {
                const m = msg as { command?: string };
                if (m?.command === "get_auth_token") {
                    if (cb) cb({ auth_token: "tok_feedback_9" });
                    return Promise.resolve({ auth_token: "tok_feedback_9" });
                }
                if (cb) cb({ status: "ok" });
                return Promise.resolve({ status: "ok" });
            },
        );

        // Seed one queued feedback item.
        fake.storage.local.__reset({
            pending_feedback: [
                {
                    description: "queued bug report that needs re-sending",
                    include_logs: false,
                    user_agent: "test-agent/1.0",
                    app_version: "0.2.1",
                    timestamp: 1_700_000_000,
                },
            ],
        });

        const fetchCalls: Array<{ url: string; init?: RequestInit }> = [];
        globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
            fetchCalls.push({ url: String(input), init });
            return Promise.resolve(new Response("{}", { status: 200 }));
        }) as typeof fetch;

        const container = document.createElement("div");
        document.body.appendChild(container);
        const root = createRoot(container);
        await act(async () => {
            root.render(React.createElement(CortexPopup));
        });
        // The drain useEffect fetches the token then POSTs each item.
        for (let i = 0; i < 10; i++) {
            await act(async () => {
                await new Promise((r) => setTimeout(r, 10));
            });
            if (fetchCalls.some((c) => c.url.includes("/api/feedback"))) break;
        }

        const feedbackFetch = fetchCalls.find((c) =>
            c.url.includes("/api/feedback"),
        );
        expect(feedbackFetch).toBeDefined();
        expect(feedbackFetch!.init?.method).toBe("POST");
        const headers =
            (feedbackFetch!.init?.headers as Record<string, string>) || {};
        expect(headers["X-Cortex-Auth-Token"]).toBe("tok_feedback_9");
        expect(headers["Content-Type"]).toBe("application/json");

        await act(async () => {
            root.unmount();
        });
        container.remove();
    }, 15000);
});
