/**
 * The intervention card is the same whether the popup was open when the
 * trigger arrived or was opened afterwards.
 *
 * The usual way to see a proposal is to notice the badge and open the
 * popup, which hydrates the mounted plan through GET_STATE. That path
 * adopted the actions, tabs and steps but skipped the explanation, the
 * level and the causal signals, which only the live INTERVENTION_TRIGGER
 * handler set. Opened from the badge, the card therefore had no causal
 * explanation, no "Why?" control, and — because the level stayed
 * ``overlay_only`` — no 👍/👎 rating for a guided or simplified proposal.
 */

import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import CortexPopup from "../popup";

const PLAN: Record<string, unknown> = {
    intervention_id: "iv-hydrate",
    level: "guided_mode",
    causal_explanation: "Tab switching rose to 14 per minute over the last 10 minutes.",
    causal_signals: [
        { name: "tab_switch_rate", value: 14, baseline: 4, direction: "above" },
    ],
    suggested_actions: [
        {
            action_id: "a1",
            action_type: "close_tab",
            label: "Close unrelated tabs",
            category: "recommended",
        },
    ],
    micro_steps: ["Write the first sentence"],
    execution_mode: "suggest_only",
};

let cleanup: (() => Promise<void>) | null = null;

afterEach(async () => {
    if (cleanup) await cleanup();
    cleanup = null;
    document.body.innerHTML = "";
});

type BgListener = (msg: Record<string, unknown>) => void;

async function mountPopup(intervention: Record<string, unknown> | null) {
    const fake = globalThis.__cortexChrome;
    fake.runtime.sendMessage.mockImplementation(
        (message: Record<string, unknown>, callback?: (value: unknown) => void) => {
            if (message.type === "GET_STATE") {
                callback?.({ connected: true, state: null, intervention, focusSession: null });
            } else if (message.type === "GET_CACHED_RECAP") {
                callback?.({ recap: null, timestamp: null });
            } else {
                callback?.(undefined);
            }
            return Promise.resolve(undefined);
        },
    );
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    await act(async () => root.render(<CortexPopup />));
    await act(async () => { await new Promise((r) => setTimeout(r, 20)); });
    cleanup = async () => {
        await act(async () => root.unmount());
        container.remove();
    };
    const calls = fake.runtime.onMessage.addListener.mock.calls;
    return { container, listener: calls[calls.length - 1][0] as BgListener };
}

function whyButton(container: HTMLElement): HTMLElement | null {
    return container.querySelector('[aria-label="Show structured causal rationale"]');
}

describe("popup intervention hydrated from GET_STATE", () => {
    it("shows the explanation and the Why? control like a live trigger", async () => {
        const { container } = await mountPopup(PLAN);

        expect(container.textContent).toContain("Tab switching rose to 14 per minute");
        expect(whyButton(container)).not.toBeNull();
    });

    it("renders the same card as the live trigger path", async () => {
        const live = await mountPopup(null);
        await act(async () => live.listener({ type: "INTERVENTION_TRIGGER", payload: PLAN }));
        await act(async () => { await new Promise((r) => setTimeout(r, 20)); });
        const liveText = live.container.textContent;
        await cleanup?.();
        cleanup = null;
        document.body.innerHTML = "";

        const hydrated = await mountPopup(PLAN);
        expect(hydrated.container.textContent).toBe(liveText);
    });
});
