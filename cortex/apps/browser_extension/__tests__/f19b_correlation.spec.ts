/**
 * F19b: correlation IDs in the browser extension.
 *
 * - newCorrelationId() returns the `cid_<12-hex>` shape.
 * - A popup-initiated USER_ACTION carries the inbound cid through to
 *   the outbound WS frame so the daemon can stitch the chain together.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { isCorrelationId, newCorrelationId } from "../lib/correlation";
import { getLatestSocket } from "../test/mocks/websocket";

interface WSFrame {
    type: string;
    payload: Record<string, unknown>;
    timestamp: number;
    sequence: number;
    correlation_id?: string;
}

type BgListener = (
    msg: Record<string, unknown>,
    sender: unknown,
    sendResponse: (resp: unknown) => void,
) => unknown;

describe("F19b correlation IDs", () => {
    beforeEach(() => {
        vi.resetModules();
    });

    it("newCorrelationId returns the cid_<12hex> shape", () => {
        const id = newCorrelationId();
        expect(isCorrelationId(id)).toBe(true);
        expect(id).toMatch(/^cid_[0-9a-f]{12}$/);
    });

    it("background propagates a popup-supplied cid onto the WS frame", async () => {
        await import("../background");
        await new Promise((r) => setTimeout(r, 0));
        const sock = getLatestSocket();
        expect(sock).not.toBeNull();

        const fake = globalThis.__cortexChrome;
        const listener = fake.runtime.onMessage.addListener.mock.calls[0][0] as BgListener;

        // First mount an intervention (so cid resolution on dismiss has a
        // baseline). The popup synthesises its own cid for USER_ACTION;
        // verify it lands on the WS frame regardless of any active plan.
        sock!.__deliver({
            type: "INTERVENTION_TRIGGER",
            payload: { intervention_id: "iv1", suggested_actions: [] },
            timestamp: Date.now() / 1000,
            sequence: 0,
            correlation_id: "iv_iv1_0",
        });

        const popupCid = "cid_abcdef012345";
        sock!.sent.length = 0;
        listener(
            {
                type: "USER_ACTION",
                action: "dismissed",
                intervention_id: "iv1",
                correlation_id: popupCid,
            },
            undefined,
            () => {},
        );

        const frames = sock!.sent.map((s) => JSON.parse(s) as WSFrame);
        const userAction = frames.find((f) => f.type === "USER_ACTION");
        expect(userAction).toBeDefined();
        expect(userAction!.correlation_id).toBe(popupCid);
    });
});
