/**
 * Overlays never outlive their intervention: restore, the daemon's
 * DISMISS_OVERLAY cue, the Cmd+Shift+D command (even offline), and a popup
 * dismissal all inject the self-contained remover into every reachable tab.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { getLatestSocket } from "../test/mocks/websocket";

type Listener = (
    message: Record<string, unknown>,
    sender: unknown,
    sendResponse: (response: unknown) => void,
) => unknown;

const TABS = [
    { id: 1, url: "https://a.example/page" },
    { id: 2, url: "chrome://extensions" },
    { id: 3, url: "https://b.example/page", incognito: true },
    { id: 4, url: "http://c.example/" },
];

function removerCalls(): number[] {
    return globalThis.__cortexChrome.scripting.executeScript.mock.calls
        .map((call) => call[0] as { target: { tabId: number }; func: { name: string } })
        .filter((injection) => injection.func.name === "removeCortexOverlay")
        .map((injection) => injection.target.tabId);
}

async function boot() {
    vi.resetModules();
    globalThis.__cortexChrome.tabs.query.mockResolvedValue(TABS);
    await import("../background");
    await new Promise((r) => setTimeout(r, 0));
    const socket = getLatestSocket()!;
    if (socket.readyState === WebSocket.CONNECTING) socket.__open();
    globalThis.__cortexChrome.scripting.executeScript.mockClear();
    const listener = globalThis.__cortexChrome.runtime.onMessage.addListener.mock
        .calls[0][0] as Listener;
    return { socket, listener };
}

async function settle(): Promise<void> {
    for (let i = 0; i < 6; i++) await new Promise((r) => setTimeout(r, 0));
}

describe("overlay removal", () => {
    beforeEach(() => {
        vi.resetModules();
    });

    it("removes page panels on INTERVENTION_RESTORE, skipping chrome:// and incognito tabs", async () => {
        const { socket } = await boot();
        await socket.__deliver({
            type: "INTERVENTION_RESTORE",
            payload: { intervention_id: "iv-1", user_action: "timed_out" },
            sequence: 5,
        });
        await settle();
        expect(removerCalls().sort()).toEqual([1, 4]);
    });

    it("consumes the daemon's DISMISS_OVERLAY cue and tells the popup", async () => {
        const { socket } = await boot();
        globalThis.__cortexChrome.runtime.sendMessage.mockClear();
        await socket.__deliver({
            type: "DISMISS_OVERLAY",
            payload: { intervention_id: "iv-1", reason: "user_shortcut" },
            sequence: 6,
        });
        await settle();
        expect(removerCalls()).toHaveLength(2);
        const dismissed = globalThis.__cortexChrome.runtime.sendMessage.mock.calls
            .map((call) => call[0] as { type?: string; intervention_id?: string })
            .find((m) => m.type === "OVERLAY_DISMISSED");
        expect(dismissed?.intervention_id).toBe("iv-1");
    });

    it("Cmd+Shift+D removes panels even while disconnected", async () => {
        const { socket } = await boot();
        socket.__remoteClose(1006, "gone");
        await settle();
        globalThis.__cortexChrome.scripting.executeScript.mockClear();
        globalThis.__cortexChrome.commands.onCommand.__dispatch("dismiss-overlay");
        await settle();
        expect(removerCalls()).toHaveLength(2);
    });

    it("a popup dismissal removes the page panel too", async () => {
        const { listener } = await boot();
        listener({ type: "USER_ACTION", action: "dismissed", intervention_id: "iv-1" }, {}, () => undefined);
        await settle();
        expect(removerCalls()).toHaveLength(2);
    });
});
