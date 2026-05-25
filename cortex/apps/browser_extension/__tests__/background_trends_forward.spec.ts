/**
 * P0 §3.2 — REQUEST_TRENDS chrome.runtime handler forwards caller's
 * window/refresh fields onto the WS frame.
 *
 * The popup may eventually offer a "Last 30 days" toggle or a
 * pull-to-refresh gesture; the background script's chrome.runtime
 * handler for REQUEST_TRENDS must thread those parameters straight
 * through to the daemon. Previously the handler hardcoded
 * ``{window: "week", refresh: false}`` which silently downgraded any
 * caller-supplied values.
 *
 * Contract:
 *   - ``message.window = "month"`` reaches the WS payload.
 *   - ``message.refresh = true`` reaches the WS payload.
 *   - Defaults remain ``"week"`` / ``false`` when fields are absent so
 *     existing callers retain their behaviour.
 *   - Unknown ``window`` values are coerced to the safe default
 *     (``"week"``) — the wire schema narrows to that union.
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
    // Drive the AUTH handshake to completion so ``connected`` is true.
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

/**
 * Dispatch a REQUEST_TRENDS message through the popup-facing
 * chrome.runtime.onMessage listener that background.ts registers.
 */
function dispatchPopupMessage(
    msg: Record<string, unknown>,
): Promise<unknown> {
    const fake = globalThis.__cortexChrome;
    const calls = fake.runtime.onMessage.addListener.mock.calls;
    return new Promise((resolve) => {
        const listener = calls[calls.length - 1][0] as (
            m: Record<string, unknown>,
            sender: Record<string, unknown>,
            sendResponse: (resp?: unknown) => void,
        ) => boolean | undefined;
        listener(msg, {}, (resp) => resolve(resp));
    });
}

describe("P0 §3.2 — REQUEST_TRENDS handler forwards window/refresh", () => {
    beforeEach(() => {
        vi.resetModules();
    });

    it("forwards window='month' onto the WS payload", async () => {
        const { sock } = await setup();
        const sentBefore = sock.sent.length;

        await dispatchPopupMessage({
            type: "REQUEST_TRENDS",
            window: "month",
            refresh: false,
        });
        for (let i = 0; i < 5; i++) {
            await new Promise((r) => setTimeout(r, 0));
        }

        const newSends = sock.sent
            .slice(sentBefore)
            .map((raw) => JSON.parse(raw) as {
                type?: string;
                payload?: Record<string, unknown>;
            });
        const trendsSend = newSends.find((m) => m.type === "REQUEST_TRENDS");
        expect(trendsSend).toBeDefined();
        expect(trendsSend!.payload?.window).toBe("month");
        expect(trendsSend!.payload?.refresh).toBe(false);
    });

    it("forwards refresh=true onto the WS payload", async () => {
        const { sock } = await setup();
        const sentBefore = sock.sent.length;

        await dispatchPopupMessage({
            type: "REQUEST_TRENDS",
            window: "week",
            refresh: true,
        });
        for (let i = 0; i < 5; i++) {
            await new Promise((r) => setTimeout(r, 0));
        }

        const newSends = sock.sent
            .slice(sentBefore)
            .map((raw) => JSON.parse(raw) as {
                type?: string;
                payload?: Record<string, unknown>;
            });
        const trendsSend = newSends.find((m) => m.type === "REQUEST_TRENDS");
        expect(trendsSend).toBeDefined();
        expect(trendsSend!.payload?.refresh).toBe(true);
    });

    it("forwards both fields (window='month', refresh=true) together", async () => {
        const { sock } = await setup();
        const sentBefore = sock.sent.length;

        await dispatchPopupMessage({
            type: "REQUEST_TRENDS",
            window: "month",
            refresh: true,
        });
        for (let i = 0; i < 5; i++) {
            await new Promise((r) => setTimeout(r, 0));
        }

        const newSends = sock.sent
            .slice(sentBefore)
            .map((raw) => JSON.parse(raw) as {
                type?: string;
                payload?: Record<string, unknown>;
            });
        const trendsSend = newSends.find((m) => m.type === "REQUEST_TRENDS");
        expect(trendsSend).toBeDefined();
        expect(trendsSend!.payload?.window).toBe("month");
        expect(trendsSend!.payload?.refresh).toBe(true);
    });

    it("defaults to window='week' and refresh=false when fields are absent", async () => {
        const { sock } = await setup();
        const sentBefore = sock.sent.length;

        await dispatchPopupMessage({ type: "REQUEST_TRENDS" });
        for (let i = 0; i < 5; i++) {
            await new Promise((r) => setTimeout(r, 0));
        }

        const newSends = sock.sent
            .slice(sentBefore)
            .map((raw) => JSON.parse(raw) as {
                type?: string;
                payload?: Record<string, unknown>;
            });
        const trendsSend = newSends.find((m) => m.type === "REQUEST_TRENDS");
        expect(trendsSend).toBeDefined();
        expect(trendsSend!.payload?.window).toBe("week");
        expect(trendsSend!.payload?.refresh).toBe(false);
    });

    it("coerces unknown window values to the safe default ('week')", async () => {
        const { sock } = await setup();
        const sentBefore = sock.sent.length;

        await dispatchPopupMessage({
            type: "REQUEST_TRENDS",
            window: "decade",
            refresh: false,
        });
        for (let i = 0; i < 5; i++) {
            await new Promise((r) => setTimeout(r, 0));
        }

        const newSends = sock.sent
            .slice(sentBefore)
            .map((raw) => JSON.parse(raw) as {
                type?: string;
                payload?: Record<string, unknown>;
            });
        const trendsSend = newSends.find((m) => m.type === "REQUEST_TRENDS");
        expect(trendsSend).toBeDefined();
        // Daemon's WSMessage schema narrows window to "week" | "month"
        // — the handler must not propagate an unknown literal.
        expect(trendsSend!.payload?.window).toBe("week");
    });
});
