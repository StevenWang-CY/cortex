/**
 * EXT-6 / C3: the INTERVENTION_TRIGGER URL cooldown must actually
 * suppress a re-trigger for a domain the user just dismissed.
 *
 * Flow under test:
 *   1. Daemon sends INTERVENTION_TRIGGER with a populated ``trigger_url``
 *      (C3 — the daemon now stamps the active-tab URL onto the plan).
 *      The background mounts it (``cortex_active_intervention`` persisted).
 *   2. The user dismisses it. The dismiss handler records the active tab's
 *      hostname in ``dismissedUrlPatterns`` with a timestamp.
 *   3. A second INTERVENTION_TRIGGER arrives for the SAME ``trigger_url``
 *      hostname within the cooldown window. It must be DROPPED — the
 *      mounted plan must still be the first one, never the second.
 *
 * Before C3 the daemon never populated ``trigger_url`` so this cooldown
 * branch was dead: ``plan.trigger_url`` was always null, ``urlKey`` was
 * always null, and the re-trigger always fired. This test pins the
 * behaviour now that ``trigger_url`` is on the wire.
 *
 * Observable: the dismiss handler clears ``cortex_active_intervention``
 * from storage.session AND records the dismissed host in the URL
 * cooldown. A SUPPRESSED re-trigger never re-mounts, so the key stays
 * absent. A NON-suppressed trigger (different host) re-mounts, so the
 * key reappears with the new intervention id. The two cases together
 * prove the cooldown discriminates on host, not that the dispatcher
 * simply crashed.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
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

function makeTrigger(id: string, triggerUrl: string, cid: string): WSFrame {
    return {
        type: "INTERVENTION_TRIGGER",
        payload: {
            intervention_id: id,
            level: "overlay_only",
            headline: "Take a breath",
            suggested_actions: [],
            trigger_url: triggerUrl,
        },
        timestamp: Date.now() / 1000,
        sequence: 0,
        correlation_id: cid,
    };
}

async function setup(): Promise<{
    sock: NonNullable<ReturnType<typeof getLatestSocket>>;
    listener: BgListener;
}> {
    vi.resetModules();
    await import("../background");
    await new Promise((r) => setTimeout(r, 0));
    const sock = getLatestSocket();
    if (!sock) throw new Error("WebSocket fake not installed");
    sock.__open();
    const fake = globalThis.__cortexChrome;
    const listener = fake.runtime.onMessage.addListener.mock
        .calls[0][0] as BgListener;
    return { sock, listener };
}

function mountedId(): string | undefined {
    const session = globalThis.__cortexChrome.storage.session.__peek();
    const plan = session.cortex_active_intervention as
        | { intervention_id?: string }
        | undefined;
    return plan?.intervention_id;
}

describe("EXT-6 INTERVENTION_TRIGGER URL cooldown", () => {
    beforeEach(() => {
        vi.resetModules();
    });

    it("suppresses a re-trigger for a dismissed url within cooldown", async () => {
        const { sock, listener } = await setup();
        const fake = globalThis.__cortexChrome;
        // The dismiss handler reads the active tab url to key the cooldown.
        fake.tabs.query.mockResolvedValue([
            { id: 7, url: "https://example.com/article", active: true },
        ]);

        // 1. First trigger for example.com — mounts.
        sock.__deliver(makeTrigger("i1", "https://example.com/article", "cid-1"));
        await new Promise((r) => setTimeout(r, 0));
        expect(mountedId()).toBe("i1");

        // 2. User dismisses it → records example.com in the URL cooldown.
        listener(
            {
                type: "USER_ACTION",
                action: "dismissed",
                intervention_id: "i1",
            },
            undefined,
            () => {},
        );
        // Let the tabs.query().then() that records the URL cooldown resolve.
        await new Promise((r) => setTimeout(r, 0));
        await new Promise((r) => setTimeout(r, 0));

        // After dismiss the active intervention is cleared.
        expect(mountedId()).toBeUndefined();

        // 3. Second trigger for the SAME hostname must be dropped — it
        // must NOT re-mount, so the cleared key stays absent.
        sock.__deliver(makeTrigger("i2", "https://example.com/other", "cid-2"));
        await new Promise((r) => setTimeout(r, 0));
        expect(mountedId()).toBeUndefined();
    });

    it("does NOT suppress a trigger for a different host", async () => {
        const { sock, listener } = await setup();
        const fake = globalThis.__cortexChrome;
        fake.tabs.query.mockResolvedValue([
            { id: 7, url: "https://example.com/article", active: true },
        ]);

        sock.__deliver(makeTrigger("i1", "https://example.com/article", "cid-1"));
        await new Promise((r) => setTimeout(r, 0));
        listener(
            {
                type: "USER_ACTION",
                action: "dismissed",
                intervention_id: "i1",
            },
            undefined,
            () => {},
        );
        await new Promise((r) => setTimeout(r, 0));
        await new Promise((r) => setTimeout(r, 0));

        // A DIFFERENT host is not on cooldown — it must mount.
        sock.__deliver(makeTrigger("i2", "https://leetcode.com/problems/x", "cid-2"));
        await new Promise((r) => setTimeout(r, 0));
        expect(mountedId()).toBe("i2");
    });
});
