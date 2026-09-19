/**
 * The worker→page path: an INTERVENTION_TRIGGER whose ui_plan asks for an
 * overlay must actually inject the panel into the active tab.
 *
 * This is the extension's central user-visible behaviour and nothing covered
 * it. `injected_motion.spec.ts` imports `injectInterventionPanel` and calls it
 * directly, so it proves the panel renders — not that anything ever injects
 * it. `overlay_removal.spec.ts` is the only spec that inspected
 * `chrome.scripting.executeScript` at all, and it filters on
 * `func.name === "removeCortexOverlay"`. The entire trigger→executeScript
 * branch could be deleted and the suite stayed green.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { getLatestSocket } from "../test/mocks/websocket";
import type { InterventionTriggerPayload } from "../types/generated/cortex_schemas";

const ACTIVE_TAB = {
    id: 7,
    url: "https://example.com/page",
    incognito: false,
    active: true,
};

const PAYLOAD = {
    intervention_id: "inject-1",
    level: "guided_mode",
    situation_summary: "Support is ready.",
    headline: "Choose one next step",
    primary_focus: "The failing test",
    micro_steps: [
        { text: "Open the failing test", status: "pending" },
        { text: "Take a deep breath", status: "pending" },
    ],
    ui_plan: { show_overlay: true, dim_background: false },
    suggested_actions: [],
    execution_mode: "suggest_only",
} satisfies InterventionTriggerPayload;

type Injection = {
    target: { tabId: number };
    func: { name: string };
    args: unknown[];
};

function panelInjections(): Injection[] {
    return globalThis.__cortexChrome.scripting.executeScript.mock.calls
        .map((call) => call[0] as Injection)
        .filter((injection) => injection.func?.name === "injectInterventionPanel");
}

async function boot(tabs: unknown[] = [ACTIVE_TAB]) {
    vi.resetModules();
    globalThis.__cortexChrome.tabs.query.mockResolvedValue(tabs);
    await import("../background");
    await new Promise((r) => setTimeout(r, 0));
    const socket = getLatestSocket()!;
    if (socket.readyState === WebSocket.CONNECTING) socket.__open();
    globalThis.__cortexChrome.scripting.executeScript.mockClear();
    return socket;
}

async function settle(): Promise<void> {
    for (let i = 0; i < 8; i += 1) await new Promise((r) => setTimeout(r, 0));
}

async function trigger(socket: ReturnType<typeof getLatestSocket>, payload: unknown) {
    await socket!.__deliver({ type: "INTERVENTION_TRIGGER", payload, sequence: 11 });
    await settle();
}

describe("intervention overlay injection", () => {
    beforeEach(() => {
        vi.resetModules();
    });

    it("injects the panel into the active tab when ui_plan asks for an overlay", async () => {
        const socket = await boot();

        await trigger(socket, PAYLOAD);

        const injections = panelInjections();
        expect(injections).toHaveLength(1);
        expect(injections[0].target.tabId).toBe(ACTIVE_TAB.id);
        // The page is handed a normalised model plus the surface CSS — never
        // the raw wire payload.
        expect(injections[0].args).toHaveLength(2);
        const model = injections[0].args[0] as {
            interventionId: string;
            steps: Array<{ text: string }>;
        };
        expect(model.interventionId).toBe("inject-1");
        // Placeholder copy is filtered by the worker before it reaches the page.
        expect(model.steps.map((step) => step.text)).toEqual([
            "Open the failing test",
        ]);
        expect(typeof injections[0].args[1]).toBe("string");
    });

    it("injects when only dim_background is set", async () => {
        const socket = await boot();

        await trigger(socket, {
            ...PAYLOAD,
            ui_plan: { show_overlay: false, dim_background: true },
        });

        expect(panelInjections()).toHaveLength(1);
    });

    it("does not inject when the ui_plan asks for no surface", async () => {
        const socket = await boot();

        await trigger(socket, {
            ...PAYLOAD,
            ui_plan: { show_overlay: false, dim_background: false },
        });

        expect(panelInjections()).toEqual([]);
    });

    it("does not inject into a restricted or incognito tab", async () => {
        const socket = await boot([
            { id: 9, url: "chrome://extensions", incognito: false, active: true },
        ]);

        await trigger(socket, PAYLOAD);

        expect(panelInjections()).toEqual([]);
    });

    it("survives an injection failure without taking the worker down", async () => {
        const socket = await boot();
        globalThis.__cortexChrome.scripting.executeScript.mockRejectedValueOnce(
            new Error("no site access"),
        );

        await trigger(socket, PAYLOAD);

        // The popup broadcast still happens; a tab Cortex cannot reach is a
        // best-effort miss, not a crash.
        expect(globalThis.__cortexChrome.runtime.sendMessage).toHaveBeenCalled();
    });
});
