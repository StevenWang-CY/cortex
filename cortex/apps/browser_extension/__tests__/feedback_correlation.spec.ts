/**
 * F16: feedback frames must echo the trigger's correlation id.
 *
 * The daemon routes USER_RATING through the same handler as USER_ACTION
 * and drops any frame whose cid differs from the one it stamped on the
 * intervention's INTERVENTION_TRIGGER. A frame sent without a cid gets a
 * freshly minted one before that check runs, so it never matches: a
 * popup 👍/👎 (or the 👎 comment) and an "opened from notification"
 * record were silently discarded. The rated intervention is often no
 * longer the mounted one — the comment box outlives Dismiss, and a
 * notification can be clicked after its panel expired — so the cid has
 * to be remembered per intervention, not read from the active slot.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { getLatestSocket } from "../test/mocks/websocket";

interface WSFrame {
    type: string;
    payload: Record<string, unknown>;
    correlation_id?: string;
}

type BgListener = (
    msg: Record<string, unknown>,
    sender: unknown,
    sendResponse: (resp: unknown) => void,
) => unknown;

function trigger(id: string, cid: string) {
    return {
        type: "INTERVENTION_TRIGGER",
        payload: {
            intervention_id: id,
            suggested_actions: [],
            tab_recommendations: { tabs: [], summary: "" },
        },
        timestamp: Date.now() / 1000,
        sequence: 0,
        correlation_id: cid,
    };
}

async function setup() {
    vi.resetModules();
    await import("../background");
    await new Promise((r) => setTimeout(r, 0));
    const sock = getLatestSocket();
    if (!sock) throw new Error("WebSocket fake not installed");
    const fake = globalThis.__cortexChrome;
    const listener = fake.runtime.onMessage.addListener.mock.calls[0][0] as BgListener;
    return { sock, listener, fake };
}

function framesOf(sent: string[], type: string): WSFrame[] {
    return sent
        .map((raw) => JSON.parse(raw) as WSFrame)
        .filter((frame) => frame.type === type);
}

describe("feedback frames echo the trigger correlation id", () => {
    beforeEach(() => {
        vi.resetModules();
    });

    it("a rating of the mounted intervention carries its trigger cid", async () => {
        const { sock, listener } = await setup();
        sock.__deliver(trigger("iv-1", "iv_iv-1_7"));
        sock.sent.length = 0;

        listener(
            { type: "USER_RATING", intervention_id: "iv-1", rating: "thumbs_up" },
            undefined,
            () => {},
        );

        const ratings = framesOf(sock.sent, "USER_RATING");
        expect(ratings).toHaveLength(1);
        expect(ratings[0].correlation_id).toBe("iv_iv-1_7");
    });

    it("a 👎 comment sent after Dismiss still carries that intervention's cid", async () => {
        const { sock, listener } = await setup();
        sock.__deliver(trigger("iv-1", "iv_iv-1_7"));
        listener(
            { type: "USER_ACTION", action: "dismissed", intervention_id: "iv-1" },
            undefined,
            () => {},
        );
        sock.sent.length = 0;

        listener(
            {
                type: "USER_RATING",
                intervention_id: "iv-1",
                rating: "thumbs_down",
                context: "not now",
            },
            undefined,
            () => {},
        );

        const ratings = framesOf(sock.sent, "USER_RATING");
        expect(ratings).toHaveLength(1);
        expect(ratings[0].correlation_id).toBe("iv_iv-1_7");
    });

    it("a rating of a superseded intervention keeps its own cid", async () => {
        const { sock, listener } = await setup();
        sock.__deliver(trigger("iv-1", "iv_iv-1_7"));
        sock.__deliver(trigger("iv-2", "iv_iv-2_9"));
        sock.sent.length = 0;

        listener(
            { type: "USER_RATING", intervention_id: "iv-1", rating: "thumbs_up" },
            undefined,
            () => {},
        );

        expect(framesOf(sock.sent, "USER_RATING")[0].correlation_id).toBe("iv_iv-1_7");
    });

    it("an OS notification click records the open with the trigger cid", async () => {
        const { sock, fake } = await setup();
        sock.__deliver(trigger("iv-1", "iv_iv-1_7"));
        sock.sent.length = 0;

        fake.notifications.onClicked.__dispatch("cortex_intervention_iv-1");
        fake.notifications.onButtonClicked.__dispatch("cortex_intervention_iv-1", 0);

        const opened = framesOf(sock.sent, "USER_ACTION").filter(
            (frame) => frame.payload.action === "os_notification_opened",
        );
        expect(opened).toHaveLength(2);
        for (const frame of opened) {
            expect(frame.correlation_id).toBe("iv_iv-1_7");
        }
    });
});
