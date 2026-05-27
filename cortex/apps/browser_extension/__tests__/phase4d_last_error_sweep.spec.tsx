/**
 * Phase 4d Task B — chrome.runtime.lastError sweep in popup.tsx.
 *
 * Wraps every ``chrome.runtime.sendMessage`` callback via the
 * ``safeSendMessage`` helper. When the background SW is evicted
 * mid-call, ``chrome.runtime.lastError`` populates inside the callback
 * — the helper must:
 *   * route the failure to the installed sink instead of throwing
 *   * NOT invoke the user-supplied callback with stale data
 *   * forward the response normally when no lastError is set
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

let safeSendMessage: (
    msg: Record<string, unknown>,
    cb?: (r: unknown) => void,
) => void;
let __setLastErrorSink: (fn: ((msg: string) => void) | null) => void;

describe("Phase 4d Task B — safeSendMessage lastError handling", () => {
    beforeEach(async () => {
        vi.resetModules();
        const mod = await import("../popup");
        safeSendMessage = (mod as unknown as {
            safeSendMessage: typeof safeSendMessage;
        }).safeSendMessage;
        __setLastErrorSink = (mod as unknown as {
            __setLastErrorSink: typeof __setLastErrorSink;
        }).__setLastErrorSink;
    });

    afterEach(() => {
        if (__setLastErrorSink) __setLastErrorSink(null);
        const fake = globalThis.__cortexChrome;
        if (fake) {
            (fake.runtime as unknown as { lastError: undefined }).lastError =
                undefined;
        }
    });

    it("routes lastError to the installed sink and skips the user cb", () => {
        const fake = globalThis.__cortexChrome;
        fake.runtime.sendMessage.mockImplementation(
            (_msg: unknown, cb?: (r: unknown) => void) => {
                (
                    fake.runtime as unknown as {
                        lastError: { message: string };
                    }
                ).lastError = {
                    message:
                        "Could not establish connection. Receiving end does not exist.",
                };
                if (cb) cb(undefined);
                return Promise.resolve(undefined);
            },
        );

        const sink = vi.fn();
        const userCb = vi.fn();
        __setLastErrorSink(sink);

        safeSendMessage({ type: "GET_STATE" }, userCb);

        expect(sink).toHaveBeenCalledTimes(1);
        expect(sink.mock.calls[0][0]).toMatch(/Could not establish/);
        expect(userCb).not.toHaveBeenCalled();
    });

    it("forwards the response normally when lastError is not set", () => {
        const fake = globalThis.__cortexChrome;
        fake.runtime.sendMessage.mockImplementation(
            (_msg: unknown, cb?: (r: unknown) => void) => {
                (fake.runtime as unknown as { lastError: undefined })
                    .lastError = undefined;
                if (cb) cb({ ok: true, value: 42 });
                return Promise.resolve(undefined);
            },
        );

        const sink = vi.fn();
        const userCb = vi.fn();
        __setLastErrorSink(sink);

        safeSendMessage({ type: "GET_STATE" }, userCb);

        expect(sink).not.toHaveBeenCalled();
        expect(userCb).toHaveBeenCalledTimes(1);
        expect(userCb.mock.calls[0][0]).toEqual({ ok: true, value: 42 });
    });

    it("routes synchronous throws to the sink", () => {
        const fake = globalThis.__cortexChrome;
        fake.runtime.sendMessage.mockImplementation(() => {
            throw new Error("extension context invalidated");
        });

        const sink = vi.fn();
        __setLastErrorSink(sink);

        // Must not propagate.
        expect(() =>
            safeSendMessage({ type: "GET_STATE" }, vi.fn()),
        ).not.toThrow();
        expect(sink).toHaveBeenCalled();
    });
});
